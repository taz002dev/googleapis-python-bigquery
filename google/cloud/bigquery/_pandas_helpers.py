# Copyright 2019 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Shared helper functions for connecting BigQuery and pandas."""

import concurrent.futures
import functools
import logging
import queue
import warnings

from packaging import version

try:
    import pandas
except ImportError:  # pragma: NO COVER
    pandas = None

try:
    import pyarrow
    import pyarrow.parquet
except ImportError:  # pragma: NO COVER
    pyarrow = None

try:
    from google.cloud.bigquery_storage import ArrowSerializationOptions
except ImportError:
    _ARROW_COMPRESSION_SUPPORT = False
else:
    # Having BQ Storage available implies that pyarrow >=1.0.0 is available, too.
    _ARROW_COMPRESSION_SUPPORT = True

from google.cloud.bigquery import schema


_LOGGER = logging.getLogger(__name__)

_NO_BQSTORAGE_ERROR = (
    "The google-cloud-bigquery-storage library is not installed, "
    "please install google-cloud-bigquery-storage to use bqstorage features."
)

_PROGRESS_INTERVAL = 0.2  # Maximum time between download status checks, in seconds.

_MAX_QUEUE_SIZE_DEFAULT = object()  # max queue size sentinel for BQ Storage downloads

_PANDAS_DTYPE_TO_BQ = {
    "bool": "BOOLEAN",
    "datetime64[ns, UTC]": "TIMESTAMP",
    # BigQuery does not support uploading DATETIME values from Parquet files.
    # See: https://github.com/googleapis/google-cloud-python/issues/9996
    "datetime64[ns]": "TIMESTAMP",
    "float32": "FLOAT",
    "float64": "FLOAT",
    "int8": "INTEGER",
    "int16": "INTEGER",
    "int32": "INTEGER",
    "int64": "INTEGER",
    "uint8": "INTEGER",
    "uint16": "INTEGER",
    "uint32": "INTEGER",
}


class _DownloadState(object):
    """Flag to indicate that a thread should exit early."""

    def __init__(self):
        # No need for a lock because reading/replacing a variable is defined to
        # be an atomic operation in the Python language definition (enforced by
        # the global interpreter lock).
        self.done = False


def pyarrow_datetime():
    return pyarrow.timestamp("us", tz=None)


def pyarrow_numeric():
    return pyarrow.decimal128(38, 9)


def pyarrow_bignumeric():
    return pyarrow.decimal256(76, 38)


def pyarrow_time():
    return pyarrow.time64("us")


def pyarrow_timestamp():
    return pyarrow.timestamp("us", tz="UTC")


if pyarrow:
    # This dictionary is duplicated in bigquery_storage/test/unite/test_reader.py
    # When modifying it be sure to update it there as well.
    BQ_TO_ARROW_SCALARS = {
        "BOOL": pyarrow.bool_,
        "BOOLEAN": pyarrow.bool_,
        "BYTES": pyarrow.binary,
        "DATE": pyarrow.date32,
        "DATETIME": pyarrow_datetime,
        "FLOAT": pyarrow.float64,
        "FLOAT64": pyarrow.float64,
        "GEOGRAPHY": pyarrow.string,
        "INT64": pyarrow.int64,
        "INTEGER": pyarrow.int64,
        "NUMERIC": pyarrow_numeric,
        "STRING": pyarrow.string,
        "TIME": pyarrow_time,
        "TIMESTAMP": pyarrow_timestamp,
    }
    ARROW_SCALAR_IDS_TO_BQ = {
        # https://arrow.apache.org/docs/python/api/datatypes.html#type-classes
        pyarrow.bool_().id: "BOOL",
        pyarrow.int8().id: "INT64",
        pyarrow.int16().id: "INT64",
        pyarrow.int32().id: "INT64",
        pyarrow.int64().id: "INT64",
        pyarrow.uint8().id: "INT64",
        pyarrow.uint16().id: "INT64",
        pyarrow.uint32().id: "INT64",
        pyarrow.uint64().id: "INT64",
        pyarrow.float16().id: "FLOAT64",
        pyarrow.float32().id: "FLOAT64",
        pyarrow.float64().id: "FLOAT64",
        pyarrow.time32("ms").id: "TIME",
        pyarrow.time64("ns").id: "TIME",
        pyarrow.timestamp("ns").id: "TIMESTAMP",
        pyarrow.date32().id: "DATE",
        pyarrow.date64().id: "DATETIME",  # because millisecond resolution
        pyarrow.binary().id: "BYTES",
        pyarrow.string().id: "STRING",  # also alias for pyarrow.utf8()
        # The exact scale and precision don't matter, see below.
        pyarrow.decimal128(38, scale=9).id: "NUMERIC",
    }

    if version.parse(pyarrow.__version__) >= version.parse("3.0.0"):
        BQ_TO_ARROW_SCALARS["BIGNUMERIC"] = pyarrow_bignumeric
        # The exact decimal's scale and precision are not important, as only
        # the type ID matters, and it's the same for all decimal256 instances.
        ARROW_SCALAR_IDS_TO_BQ[pyarrow.decimal256(76, scale=38).id] = "BIGNUMERIC"
        _BIGNUMERIC_SUPPORT = True
    else:
        _BIGNUMERIC_SUPPORT = False

else:  # pragma: NO COVER
    BQ_TO_ARROW_SCALARS = {}  # pragma: NO COVER
    ARROW_SCALAR_IDS_TO_BQ = {}  # pragma: NO_COVER
    _BIGNUMERIC_SUPPORT = False  # pragma: NO COVER


def bq_to_arrow_struct_data_type(field):
    arrow_fields = []
    for subfield in field.fields:
        arrow_subfield = bq_to_arrow_field(subfield)
        if arrow_subfield:
            arrow_fields.append(arrow_subfield)
        else:
            # Could not determine a subfield type. Fallback to type
            # inference.
            return None
    return pyarrow.struct(arrow_fields)


def bq_to_arrow_data_type(field):
    """Return the Arrow data type, corresponding to a given BigQuery column.

    Returns:
        None: if default Arrow type inspection should be used.
    """
    if field.mode is not None and field.mode.upper() == "REPEATED":
        inner_type = bq_to_arrow_data_type(
            schema.SchemaField(field.name, field.field_type, fields=field.fields)
        )
        if inner_type:
            return pyarrow.list_(inner_type)
        return None

    field_type_upper = field.field_type.upper() if field.field_type else ""
    if field_type_upper in schema._STRUCT_TYPES:
        return bq_to_arrow_struct_data_type(field)

    data_type_constructor = BQ_TO_ARROW_SCALARS.get(field_type_upper)
    if data_type_constructor is None:
        return None
    return data_type_constructor()


def bq_to_arrow_field(bq_field):
    """Return the Arrow field, corresponding to a given BigQuery column.

    Returns:
        None: if the Arrow type cannot be determined.
    """
    arrow_type = bq_to_arrow_data_type(bq_field)
    if arrow_type:
        is_nullable = bq_field.mode.upper() == "NULLABLE"
        return pyarrow.field(bq_field.name, arrow_type, nullable=is_nullable)

    warnings.warn("Unable to determine type for field '{}'.".format(bq_field.name))
    return None


def bq_to_arrow_schema(bq_schema):
    """Return the Arrow schema, corresponding to a given BigQuery schema.

    Returns:
        None: if any Arrow type cannot be determined.
    """
    arrow_fields = []
    for bq_field in bq_schema:
        arrow_field = bq_to_arrow_field(bq_field)
        if arrow_field is None:
            # Auto-detect the schema if there is an unknown field type.
            return None
        arrow_fields.append(arrow_field)
    return pyarrow.schema(arrow_fields)


def bq_to_arrow_array(series, bq_field):
    arrow_type = bq_to_arrow_data_type(bq_field)

    field_type_upper = bq_field.field_type.upper() if bq_field.field_type else ""

    if bq_field.mode.upper() == "REPEATED":
        return pyarrow.ListArray.from_pandas(series, type=arrow_type)
    if field_type_upper in schema._STRUCT_TYPES:
        return pyarrow.StructArray.from_pandas(series, type=arrow_type)
    return pyarrow.Array.from_pandas(series, type=arrow_type)


def get_column_or_index(dataframe, name):
    """Return a column or index as a pandas series."""
    if name in dataframe.columns:
        return dataframe[name].reset_index(drop=True)

    if isinstance(dataframe.index, pandas.MultiIndex):
        if name in dataframe.index.names:
            return (
                dataframe.index.get_level_values(name)
                .to_series()
                .reset_index(drop=True)
            )
    else:
        if name == dataframe.index.name:
            return dataframe.index.to_series().reset_index(drop=True)

    raise ValueError("column or index '{}' not found.".format(name))


def list_columns_and_indexes(dataframe):
    """Return all index and column names with dtypes.

    Returns:
        Sequence[Tuple[str, dtype]]:
            Returns a sorted list of indexes and column names with
            corresponding dtypes. If an index is missing a name or has the
            same name as a column, the index is omitted.
    """
    column_names = frozenset(dataframe.columns)
    columns_and_indexes = []
    if isinstance(dataframe.index, pandas.MultiIndex):
        for name in dataframe.index.names:
            if name and name not in column_names:
                values = dataframe.index.get_level_values(name)
                columns_and_indexes.append((name, values.dtype))
    else:
        if dataframe.index.name and dataframe.index.name not in column_names:
            columns_and_indexes.append((dataframe.index.name, dataframe.index.dtype))

    columns_and_indexes += zip(dataframe.columns, dataframe.dtypes)
    return columns_and_indexes


def dataframe_to_bq_schema(dataframe, bq_schema):
    """Convert a pandas DataFrame schema to a BigQuery schema.

    Args:
        dataframe (pandas.DataFrame):
            DataFrame for which the client determines the BigQuery schema.
        bq_schema (Sequence[Union[ \
            :class:`~google.cloud.bigquery.schema.SchemaField`, \
            Mapping[str, Any] \
        ]]):
            A BigQuery schema. Use this argument to override the autodetected
            type for some or all of the DataFrame columns.

    Returns:
        Optional[Sequence[google.cloud.bigquery.schema.SchemaField]]:
            The automatically determined schema. Returns None if the type of
            any column cannot be determined.
    """
    if bq_schema:
        bq_schema = schema._to_schema_fields(bq_schema)
        bq_schema_index = {field.name: field for field in bq_schema}
        bq_schema_unused = set(bq_schema_index.keys())
    else:
        bq_schema_index = {}
        bq_schema_unused = set()

    bq_schema_out = []
    unknown_type_fields = []

    for column, dtype in list_columns_and_indexes(dataframe):
        # Use provided type from schema, if present.
        bq_field = bq_schema_index.get(column)
        if bq_field:
            bq_schema_out.append(bq_field)
            bq_schema_unused.discard(bq_field.name)
            continue

        # Otherwise, try to automatically determine the type based on the
        # pandas dtype.
        bq_type = _PANDAS_DTYPE_TO_BQ.get(dtype.name)
        bq_field = schema.SchemaField(column, bq_type)
        bq_schema_out.append(bq_field)

        if bq_field.field_type is None:
            unknown_type_fields.append(bq_field)

    # Catch any schema mismatch. The developer explicitly asked to serialize a
    # column, but it was not found.
    if bq_schema_unused:
        raise ValueError(
            u"bq_schema contains fields not present in dataframe: {}".format(
                bq_schema_unused
            )
        )

    # If schema detection was not successful for all columns, also try with
    # pyarrow, if available.
    if unknown_type_fields:
        if not pyarrow:
            msg = u"Could not determine the type of columns: {}".format(
                ", ".join(field.name for field in unknown_type_fields)
            )
            warnings.warn(msg)
            return None  # We cannot detect the schema in full.

        # The augment_schema() helper itself will also issue unknown type
        # warnings if detection still fails for any of the fields.
        bq_schema_out = augment_schema(dataframe, bq_schema_out)

    return tuple(bq_schema_out) if bq_schema_out else None


def augment_schema(dataframe, current_bq_schema):
    """Try to deduce the unknown field types and return an improved schema.

    This function requires ``pyarrow`` to run. If all the missing types still
    cannot be detected, ``None`` is returned. If all types are already known,
    a shallow copy of the given schema is returned.

    Args:
        dataframe (pandas.DataFrame):
            DataFrame for which some of the field types are still unknown.
        current_bq_schema (Sequence[google.cloud.bigquery.schema.SchemaField]):
            A BigQuery schema for ``dataframe``. The types of some or all of
            the fields may be ``None``.
    Returns:
        Optional[Sequence[google.cloud.bigquery.schema.SchemaField]]
    """
    augmented_schema = []
    unknown_type_fields = []

    for field in current_bq_schema:
        if field.field_type is not None:
            augmented_schema.append(field)
            continue

        arrow_table = pyarrow.array(dataframe[field.name])
        detected_type = ARROW_SCALAR_IDS_TO_BQ.get(arrow_table.type.id)

        if detected_type is None:
            unknown_type_fields.append(field)
            continue

        new_field = schema.SchemaField(
            name=field.name,
            field_type=detected_type,
            mode=field.mode,
            description=field.description,
            fields=field.fields,
        )
        augmented_schema.append(new_field)

    if unknown_type_fields:
        warnings.warn(
            u"Pyarrow could not determine the type of columns: {}.".format(
                ", ".join(field.name for field in unknown_type_fields)
            )
        )
        return None

    return augmented_schema


def dataframe_to_arrow(dataframe, bq_schema):
    """Convert pandas dataframe to Arrow table, using BigQuery schema.

    Args:
        dataframe (pandas.DataFrame):
            DataFrame to convert to Arrow table.
        bq_schema (Sequence[Union[ \
            :class:`~google.cloud.bigquery.schema.SchemaField`, \
            Mapping[str, Any] \
        ]]):
            Desired BigQuery schema. The number of columns must match the
            number of columns in the DataFrame.

    Returns:
        pyarrow.Table:
            Table containing dataframe data, with schema derived from
            BigQuery schema.
    """
    column_names = set(dataframe.columns)
    column_and_index_names = set(
        name for name, _ in list_columns_and_indexes(dataframe)
    )

    bq_schema = schema._to_schema_fields(bq_schema)
    bq_field_names = set(field.name for field in bq_schema)

    extra_fields = bq_field_names - column_and_index_names
    if extra_fields:
        raise ValueError(
            u"bq_schema contains fields not present in dataframe: {}".format(
                extra_fields
            )
        )

    # It's okay for indexes to be missing from bq_schema, but it's not okay to
    # be missing columns.
    missing_fields = column_names - bq_field_names
    if missing_fields:
        raise ValueError(
            u"bq_schema is missing fields from dataframe: {}".format(missing_fields)
        )

    arrow_arrays = []
    arrow_names = []
    arrow_fields = []
    for bq_field in bq_schema:
        arrow_fields.append(bq_to_arrow_field(bq_field))
        arrow_names.append(bq_field.name)
        arrow_arrays.append(
            bq_to_arrow_array(get_column_or_index(dataframe, bq_field.name), bq_field)
        )

    if all((field is not None for field in arrow_fields)):
        return pyarrow.Table.from_arrays(
            arrow_arrays, schema=pyarrow.schema(arrow_fields)
        )
    return pyarrow.Table.from_arrays(arrow_arrays, names=arrow_names)


def dataframe_to_parquet(dataframe, bq_schema, filepath, parquet_compression="SNAPPY"):
    """Write dataframe as a Parquet file, according to the desired BQ schema.

    This function requires the :mod:`pyarrow` package. Arrow is used as an
    intermediate format.

    Args:
        dataframe (pandas.DataFrame):
            DataFrame to convert to Parquet file.
        bq_schema (Sequence[Union[ \
            :class:`~google.cloud.bigquery.schema.SchemaField`, \
            Mapping[str, Any] \
        ]]):
            Desired BigQuery schema. Number of columns must match number of
            columns in the DataFrame.
        filepath (str):
            Path to write Parquet file to.
        parquet_compression (Optional[str]):
            The compression codec to use by the the ``pyarrow.parquet.write_table``
            serializing method. Defaults to "SNAPPY".
            https://arrow.apache.org/docs/python/generated/pyarrow.parquet.write_table.html#pyarrow-parquet-write-table
    """
    if pyarrow is None:
        raise ValueError("pyarrow is required for BigQuery schema conversion.")

    bq_schema = schema._to_schema_fields(bq_schema)
    arrow_table = dataframe_to_arrow(dataframe, bq_schema)
    pyarrow.parquet.write_table(arrow_table, filepath, compression=parquet_compression)


def _row_iterator_page_to_arrow(page, column_names, arrow_types):
    # Iterate over the page to force the API request to get the page data.
    try:
        next(iter(page))
    except StopIteration:
        pass

    arrays = []
    for column_index, arrow_type in enumerate(arrow_types):
        arrays.append(pyarrow.array(page._columns[column_index], type=arrow_type))

    if isinstance(column_names, pyarrow.Schema):
        return pyarrow.RecordBatch.from_arrays(arrays, schema=column_names)
    return pyarrow.RecordBatch.from_arrays(arrays, names=column_names)


def download_arrow_row_iterator(pages, bq_schema):
    """Use HTTP JSON RowIterator to construct an iterable of RecordBatches.

    Args:
        pages (Iterator[:class:`google.api_core.page_iterator.Page`]):
            An iterator over the result pages.
        bq_schema (Sequence[Union[ \
            :class:`~google.cloud.bigquery.schema.SchemaField`, \
            Mapping[str, Any] \
        ]]):
            A decription of the fields in result pages.
    Yields:
        :class:`pyarrow.RecordBatch`
        The next page of records as a ``pyarrow`` record batch.
    """
    bq_schema = schema._to_schema_fields(bq_schema)
    column_names = bq_to_arrow_schema(bq_schema) or [field.name for field in bq_schema]
    arrow_types = [bq_to_arrow_data_type(field) for field in bq_schema]

    for page in pages:
        yield _row_iterator_page_to_arrow(page, column_names, arrow_types)


def _row_iterator_page_to_dataframe(page, column_names, dtypes):
    # Iterate over the page to force the API request to get the page data.
    try:
        next(iter(page))
    except StopIteration:
        pass

    columns = {}
    for column_index, column_name in enumerate(column_names):
        dtype = dtypes.get(column_name)
        columns[column_name] = pandas.Series(page._columns[column_index], dtype=dtype)

    return pandas.DataFrame(columns, columns=column_names)


def download_dataframe_row_iterator(pages, bq_schema, dtypes):
    """Use HTTP JSON RowIterator to construct a DataFrame.

    Args:
        pages (Iterator[:class:`google.api_core.page_iterator.Page`]):
            An iterator over the result pages.
        bq_schema (Sequence[Union[ \
            :class:`~google.cloud.bigquery.schema.SchemaField`, \
            Mapping[str, Any] \
        ]]):
            A decription of the fields in result pages.
        dtypes(Mapping[str, numpy.dtype]):
            The types of columns in result data to hint construction of the
            resulting DataFrame. Not all column types have to be specified.
    Yields:
        :class:`pandas.DataFrame`
        The next page of records as a ``pandas.DataFrame`` record batch.
    """
    bq_schema = schema._to_schema_fields(bq_schema)
    column_names = [field.name for field in bq_schema]
    for page in pages:
        yield _row_iterator_page_to_dataframe(page, column_names, dtypes)


def _bqstorage_page_to_arrow(page):
    return page.to_arrow()


def _bqstorage_page_to_dataframe(column_names, dtypes, page):
    # page.to_dataframe() does not preserve column order in some versions
    # of google-cloud-bigquery-storage. Access by column name to rearrange.
    return page.to_dataframe(dtypes=dtypes)[column_names]


def _download_table_bqstorage_stream(
    download_state, bqstorage_client, session, stream, worker_queue, page_to_item
):
    rowstream = bqstorage_client.read_rows(stream.name).rows(session)

    for page in rowstream.pages:
        if download_state.done:
            return
        item = page_to_item(page)
        worker_queue.put(item)


def _nowait(futures):
    """Separate finished and unfinished threads, much like
    :func:`concurrent.futures.wait`, but don't wait.
    """
    done = []
    not_done = []
    for future in futures:
        if future.done():
            done.append(future)
        else:
            not_done.append(future)
    return done, not_done


def _download_table_bqstorage(
    project_id,
    table,
    bqstorage_client,
    preserve_order=False,
    selected_fields=None,
    page_to_item=None,
    max_queue_size=_MAX_QUEUE_SIZE_DEFAULT,
):
    """Use (faster, but billable) BQ Storage API to construct DataFrame."""

    # Passing a BQ Storage client in implies that the BigQuery Storage library
    # is available and can be imported.
    from google.cloud import bigquery_storage

    if "$" in table.table_id:
        raise ValueError(
            "Reading from a specific partition is not currently supported."
        )
    if "@" in table.table_id:
        raise ValueError("Reading from a specific snapshot is not currently supported.")

    requested_streams = 1 if preserve_order else 0

    requested_session = bigquery_storage.types.ReadSession(
        table=table.to_bqstorage(), data_format=bigquery_storage.types.DataFormat.ARROW
    )
    if selected_fields is not None:
        for field in selected_fields:
            requested_session.read_options.selected_fields.append(field.name)

    if _ARROW_COMPRESSION_SUPPORT:
        requested_session.read_options.arrow_serialization_options.buffer_compression = (
            ArrowSerializationOptions.CompressionCodec.LZ4_FRAME
        )

    session = bqstorage_client.create_read_session(
        parent="projects/{}".format(project_id),
        read_session=requested_session,
        max_stream_count=requested_streams,
    )

    _LOGGER.debug(
        "Started reading table '{}.{}.{}' with BQ Storage API session '{}'.".format(
            table.project, table.dataset_id, table.table_id, session.name
        )
    )

    # Avoid reading rows from an empty table.
    if not session.streams:
        return

    total_streams = len(session.streams)

    # Use _DownloadState to notify worker threads when to quit.
    # See: https://stackoverflow.com/a/29237343/101923
    download_state = _DownloadState()

    # Create a queue to collect frames as they are created in each thread.
    #
    # The queue needs to be bounded by default, because if the user code processes the
    # fetched result pages too slowly, while at the same time new pages are rapidly being
    # fetched from the server, the queue can grow to the point where the process runs
    # out of memory.
    if max_queue_size is _MAX_QUEUE_SIZE_DEFAULT:
        max_queue_size = total_streams
    elif max_queue_size is None:
        max_queue_size = 0  # unbounded

    worker_queue = queue.Queue(maxsize=max_queue_size)

    with concurrent.futures.ThreadPoolExecutor(max_workers=total_streams) as pool:
        try:
            # Manually submit jobs and wait for download to complete rather
            # than using pool.map because pool.map continues running in the
            # background even if there is an exception on the main thread.
            # See: https://github.com/googleapis/google-cloud-python/pull/7698
            not_done = [
                pool.submit(
                    _download_table_bqstorage_stream,
                    download_state,
                    bqstorage_client,
                    session,
                    stream,
                    worker_queue,
                    page_to_item,
                )
                for stream in session.streams
            ]

            while not_done:
                # Don't block on the worker threads. For performance reasons,
                # we want to block on the queue's get method, instead. This
                # prevents the queue from filling up, because the main thread
                # has smaller gaps in time between calls to the queue's get
                # method. For a detailed explaination, see:
                # https://friendliness.dev/2019/06/18/python-nowait/
                done, not_done = _nowait(not_done)
                for future in done:
                    # Call result() on any finished threads to raise any
                    # exceptions encountered.
                    future.result()

                try:
                    frame = worker_queue.get(timeout=_PROGRESS_INTERVAL)
                    yield frame
                except queue.Empty:  # pragma: NO COVER
                    continue

            # Return any remaining values after the workers finished.
            while True:  # pragma: NO COVER
                try:
                    frame = worker_queue.get_nowait()
                    yield frame
                except queue.Empty:  # pragma: NO COVER
                    break
        finally:
            # No need for a lock because reading/replacing a variable is
            # defined to be an atomic operation in the Python language
            # definition (enforced by the global interpreter lock).
            download_state.done = True

            # Shutdown all background threads, now that they should know to
            # exit early.
            pool.shutdown(wait=True)


def download_arrow_bqstorage(
    project_id, table, bqstorage_client, preserve_order=False, selected_fields=None,
):
    return _download_table_bqstorage(
        project_id,
        table,
        bqstorage_client,
        preserve_order=preserve_order,
        selected_fields=selected_fields,
        page_to_item=_bqstorage_page_to_arrow,
    )


def download_dataframe_bqstorage(
    project_id,
    table,
    bqstorage_client,
    column_names,
    dtypes,
    preserve_order=False,
    selected_fields=None,
    max_queue_size=_MAX_QUEUE_SIZE_DEFAULT,
):
    page_to_item = functools.partial(_bqstorage_page_to_dataframe, column_names, dtypes)
    return _download_table_bqstorage(
        project_id,
        table,
        bqstorage_client,
        preserve_order=preserve_order,
        selected_fields=selected_fields,
        page_to_item=page_to_item,
        max_queue_size=max_queue_size,
    )


def dataframe_to_json_generator(dataframe):
    for row in dataframe.itertuples(index=False, name=None):
        output = {}
        for column, value in zip(dataframe.columns, row):
            # Omit NaN values.
            if value != value:
                continue
            output[column] = value
        yield output
