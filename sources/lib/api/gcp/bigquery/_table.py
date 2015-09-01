# Copyright 2014 Google Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Implements Table, and related Table BigQuery APIs."""

import codecs
import csv
from datetime import datetime, timedelta
import math
import pandas as pd
import time
from traceback import format_exc as _format_exc
import uuid

from gcp._util import Iterator as _Iterator
from gcp._util import Job as _Job
from gcp._util import JobError as _JobError
from gcp._util import RequestException as _RequestException
from gcp._util import async_method
from ._job import Job as _BQJob
from ._parser import Parser as _Parser
from ._utils import parse_table_name as _parse_table_name

# import of Query is at end of module as we have a circular dependency of
# Query.execute().results -> Table and Table.sample() -> Query


class Schema(list):
  """Represents the schema of a BigQuery table.
  """

  class _Field(object):

    # TODO(gram): consider renaming data_type member to type. Yes, it shadows top-level
    # name but that is what we are using in __str__ and __getitem__ and is what is used in BQ.
    # The shadowing is unlikely to cause problems.
    def __init__(self, name, data_type, mode='NULLABLE', description=''):
      self.name = name
      self.data_type = data_type
      self.mode = mode
      self.description = description

    def _repr_sql_(self, args=None):
      """Returns a representation of the field for embedding into a SQL statement.

      Returns:
        A formatted field name for use within SQL statements.
      """
      return self.name

    def __eq__(self, other):
      return self.name == other.name and self.data_type == other.data_type \
             and self.mode == other.mode

    def __str__(self):
      # Stringize in the form of a dictionary
      return "{ 'name': '%s', 'type': '%s', 'mode':'%s', 'description': '%s' }" % \
             (self.name, self.data_type, self.mode, self.description)

    def __repr__(self):
      return str(self)

    def __getitem__(self, item):
      # TODO(gram): Currently we need this for a Schema object to work with the Parser object.
      # Eventually if we change Parser to only work with Schema (and not also with the
      # schema dictionaries in query results) we can remove this.

      if item == 'name':
        return self.name
      if item == 'type':
        return self.data_type
      if item == 'mode':
        return self.mode
      if item == 'description':
        return self.description

  @staticmethod
  def _from_dataframe(dataframe, default_type='STRING'):
    """
      Infer a BigQuery table schema from a Pandas dataframe. Note that if you don't explicitly set
      the types of the columns in the dataframe, they may be of a type that forces coercion to
      STRING, so even though the fields in the dataframe themselves may be numeric, the type in the
      derived schema may not be. Hence it is prudent to make sure the Pandas dataframe is typed
      correctly.

    Args:
      dataframe: The DataFrame.
      default_type : The default big query type in case the type of the column does not exist in
          the schema.
    Returns:
      A list of dictionaries containing field 'name' and 'type' entries, suitable for use in a
          BigQuery Tables resource schema.
    """

    type_mapping = {
      'i': 'INTEGER',
      'b': 'BOOLEAN',
      'f': 'FLOAT',
      'O': 'STRING',
      'S': 'STRING',
      'U': 'STRING',
      'M': 'TIMESTAMP'
    }

    fields = []
    for column_name, dtype in dataframe.dtypes.iteritems():
      fields.append({'name': column_name,
                     'type': type_mapping.get(dtype.kind, default_type)})

    return fields

  @staticmethod
  def _from_list(data):
    """
    Infer a BigQuery table schema from a list. The list must be non-empty and be a list
    of dictionaries (in which case the first item is used), or a list of lists. In the latter
    case the type of the elements is used and the field names are simply 'Column1', 'Column2', etc.

    Args:
      data: The list.
    Returns:
      A list of dictionaries containing field 'name' and 'type' entries, suitable for use in a
          BigQuery Tables resource schema.
    """
    if not data:
      return []

    def _get_type(value):
      if isinstance(value, datetime):
        return 'TIMESTAMP'
      elif isinstance(value, int):
        return 'INTEGER'
      elif isinstance(value, float):
        return 'FLOAT'
      elif isinstance(value, bool):
        return 'BOOLEAN'
      else:
        return 'STRING'

    datum = data[0]
    if isinstance(datum, dict):
      return [{'name': key, 'type': _get_type(datum[key])} for key in datum.keys()]
    else:
      return [{'name': 'Column%d' % (i + 1), 'type': _get_type(datum[i])}
              for i in range(0, len(datum))]

  def __init__(self, data=None, definition=None):
    """Initializes a Schema from its raw JSON representation, a Pandas Dataframe, or a list.

    Args:
      data: A Pandas DataFrame or a list of dictionaries or lists from which to infer a schema.
      definition: a definition of the schema as a list of dictionaries with 'name' and 'type'
          entries and possibly 'mode' and 'description' entries. Only used if no data argument was
          provided. 'mode' can be 'NULLABLE', 'REQUIRED' or 'REPEATED'. For the allowed types, see:
          https://cloud.google.com/bigquery/preparing-data-for-bigquery#datatypes
    """
    list.__init__(self)
    self._map = {}
    if isinstance(data, pd.DataFrame):
      data = Schema._from_dataframe(data)
    elif isinstance(data, list):
      data = Schema._from_list(data)
    elif definition:
      data = definition
    else:
      raise Exception("Schema requires either data or json argument.")
    self._bq_schema = data
    self._populate_fields(data)

  def __getitem__(self, key):
    """Provides ability to lookup a schema field by position or by name.
    """
    if isinstance(key, basestring):
      return self._map.get(key, None)
    return list.__getitem__(self, key)

  def _add_field(self, name, data_type, mode='NULLABLE', description=''):
    field = Schema._Field(name, data_type, mode, description)
    self.append(field)
    self._map[name] = field

  def _populate_fields(self, data, prefix=''):
    for field_data in data:
      name = prefix + field_data['name']
      data_type = field_data['type']
      self._add_field(name, data_type, field_data.get('mode', None),
                      field_data.get('description', None))

      if data_type == 'RECORD':
        # Recurse into the nested fields, using this field's name as a prefix.
        self._populate_fields(field_data.get('fields'), name + '.')

  def __str__(self):
    return str(self._bq_schema)

  def __eq__(self, other):
    other_map = other._map
    if len(self._map) != len(other_map):
      return False
    for name in self._map.iterkeys():
      if name not in other_map:
        return False
      if not self._map[name] == other_map[name]:
        return False
    return True


class TableMetadata(object):
  """Represents metadata about a BigQuery table."""

  def __init__(self, table, info):
    """Initializes an instance of a TableMetadata.

    Args:
      table: the table this belongs to.
      info: The BigQuery information about this table.
    """
    self._table = table
    self._info = info

  @property
  def created_on(self):
    """The creation timestamp."""
    timestamp = self._info.get('creationTime')
    return _Parser.parse_timestamp(timestamp)

  @property
  def description(self):
    """The description of the table if it exists."""
    return self._info.get('description', '')

  @property
  def expires_on(self):
    """The timestamp for when the table will expire."""
    timestamp = self._info.get('expirationTime', None)
    if timestamp is None:
      return None
    return _Parser.parse_timestamp(timestamp)

  @property
  def friendly_name(self):
    """The friendly name of the table if it exists."""
    return self._info.get('friendlyName', '')

  @property
  def full_name(self):
    """The full name of the table."""
    return self._table.full_name

  @property
  def modified_on(self):
    """The timestamp for when the table was last modified."""
    timestamp = self._info.get('lastModifiedTime')
    return _Parser.parse_timestamp(timestamp)

  @property
  def rows(self):
    """The number of rows within the table."""
    return int(self._info['numRows']) if 'numRows' in self._info else -1

  @property
  def size(self):
    """The size of the table in bytes."""
    return int(self._info['numBytes']) if 'numBytes' in self._info else -1


class Table(object):
  """Represents a Table object referencing a BigQuery table. """

  # Allowed characters in a BigQuery table column name
  _VALID_COLUMN_NAME_CHARACTERS = '_abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'

  # When fetching table contents, the max number of rows to fetch per HTTP request
  _DEFAULT_PAGE_SIZE = 1024

  # Milliseconds per week
  _MSEC_PER_WEEK = 7 * 24 * 3600 * 1000

  def __init__(self, api, name):
    """Initializes an instance of a Table object.

    Args:
      api: the BigQuery API object to use to issue requests.
      name: the name of the table either as a string or a 3-part tuple (projectid, datasetid, name).
    """
    self._api = api
    self._name_parts = _parse_table_name(name, api.project_id)
    self._full_name = '%s:%s.%s%s' % self._name_parts
    self._info = None
    self._cached_page = None
    self._cached_page_index = 0

  @property
  def full_name(self):
    """The full name for the table."""
    return self._full_name

  @property
  def name(self):
    """The TableName for the table."""
    return self._name_parts

  @property
  def is_temporary(self):
    """ Whether this is a short-lived table or not. """
    return False

  def _load_info(self):
    """Loads metadata about this table."""
    if self._info is None:
      self._info = self._api.tables_get(self._name_parts)

  @property
  def metadata(self):
    """Retrieves metadata about the table.

    Returns:
      A TableMetadata object.
    Raises
      Exception if the request could not be executed or the response was malformed.
    """
    self._load_info()
    return TableMetadata(self, self._info)

  def exists(self):
    """Checks if the table exists.

    Returns:
      True if the table exists; False otherwise.
    Raises:
      Exception if there was an error requesting information about the table.
    """
    try:
      _ = self._api.tables_get(self._name_parts)
    except _RequestException as e:
      if e.status == 404:
        return False
      raise e
    return True

  def delete(self):
    """ Delete the table.

    Returns:
      Nothing
    """
    try:
      self._api.table_delete(self._name_parts)
    except Exception as e:
      # TODO(gram): May want to check the error reasons here and if it is not
      # because the file didn't exist, return an error.
      pass

  def create(self, schema, overwrite=False):
    """ Create the table with the specified schema.

    Args:
      schema: the schema to use to create the table. Should be a list of dictionaries, each
          containing at least a pair of entries, 'name' and 'type'.
          See https://cloud.google.com/bigquery/docs/reference/v2/tables#resource
      overwrite: if True, delete the object first if it exists. If False and the object exists,
          creation will fail and raise an Exception.
    Returns:
      The Table instance.
    Raises:
      Exception if the table couldn't be created or already exists and truncate was False.
    """
    if overwrite and self.exists():
      self.delete()
    if isinstance(schema, Schema):
      schema = schema._bq_schema
    response = self._api.tables_insert(self._name_parts, schema=schema)
    if 'selfLink' in response:
      return self
    raise Exception("Table %s could not be created as it already exists" % self.full_name)

  def sample(self, fields=None, count=5, sampling=None, use_cache=True):
    """Retrieves a sampling of data from the table.

    Args:
      fields: an optional list of field names to retrieve.
      count: an optional count of rows to retrieve which is used if a specific
          sampling is not specified.
      sampling: an optional sampling strategy to apply to the table.
      use_cache: whether to use cached results or not.
    Returns:
      A QueryResults object containing the resulting data.
    Raises:
      Exception if the sample query could not be executed or query response was malformed.
    """
    sql = self._repr_sql_()
    return _Query.sampling_query(self._api, sql, count=count, fields=fields, sampling=sampling)\
        .results(use_cache=use_cache)

  @staticmethod
  def _encode_dict_as_row(record, column_name_map):
    """ Encode a dictionary representing a table row in a form suitable for streaming to BQ.
        This means encoding timestamps as ISO-compatible strings and removing invalid
        characters from column names.

    Args:
      record: a Python dictionary representing the table row.
      column_name_map: a dictionary mapping dictionary keys to column names. This is initially
        empty and built up by this method when it first encounters each column, then used as a
        cache subsequently.
    Returns:
      The sanitized dictionary.
    """
    for k in record.keys():
      v = record[k]
      # If the column is a date, convert to ISO string.
      if isinstance(v, pd.Timestamp) or isinstance(v, datetime):
        v = record[k] = record[k].isoformat()

      # If k has invalid characters clean it up
      if k not in column_name_map:
        column_name_map[k] = ''.join(c for c in k if c in Table._VALID_COLUMN_NAME_CHARACTERS)
      new_k = column_name_map[k]
      if k != new_k:
        record[new_k] = v
        del record[k]
    return record

  def insertAll(self, data, include_index=False, index_name=None):
    """ Insert the contents of a Pandas DataFrame or a list of dictionaries into the table.

    Args:
      data: the DataFrame or list to insert.
      include_index: whether to include the DataFrame or list index as a column in the BQ table.
      index_name: for a list, if include_index is True, this should be the name for the index.
          If not specified, 'Index' will be used.
    Returns:
      The table.
    Raises:
      Exception if the table doesn't exist, the schema differs from the data's schema, or the insert
          failed.
    """
    # There are BigQuery limits on the streaming API:
    #
    # max_rows_per_post = 500
    # max_bytes_per_row = 20000
    # max_rows_per_second = 10000
    # max_bytes_per_post = 1000000
    # max_bytes_per_second = 10000000
    #
    # It is non-trivial to enforce these here, but as an approximation we enforce the 500 row limit
    # with a 0.1 sec POST interval.
    max_rows_per_post = 500
    post_interval = 0.1

    # TODO(gram): add different exception types for each failure case.
    if not self.exists():
      raise Exception('Table %s does not exist.' % self._full_name)

    data_schema = Schema(data=data)
    if isinstance(data, list):
      if include_index:
        if not index_name:
          index_name = 'Index'
        data_schema._add_field(index_name, 'INTEGER')

    table_schema = self.schema

    # Do some validation of the two schema to make sure they are compatible.
    for data_field in data_schema:
      name = data_field.name
      table_field = table_schema[name]
      if table_field is None:
        raise Exception('Table does not contain field %s' % name)
      data_type = data_field.data_type
      table_type = table_field.data_type
      if table_type != data_type:
        raise Exception('Field %s in data has type %s but in table has type %s' %
                        (name, data_type, table_type))

    total_rows = len(data)
    total_pushed = 0

    job_id = uuid.uuid4().hex
    rows = []
    column_name_map = {}

    is_dataframe = isinstance(data, pd.DataFrame)
    if is_dataframe:
      # reset_index creates a new dataframe so we don't affect the original. reset_index(drop=True)
      # drops the original index and uses an integer range.
      gen = data.reset_index(drop=not include_index).iterrows()
    else:
      gen = enumerate(data)

    for index, row in gen:
      if is_dataframe:
        row = row.to_dict()
      elif include_index:
        row[index_name] = index

      rows.append({
        'json': self._encode_dict_as_row(row, column_name_map),
        'insertId': job_id + str(index)
      })

      total_pushed += 1

      if (total_pushed == total_rows) or (len(rows) == max_rows_per_post):
        response = self._api.tabledata_insertAll(self._name_parts, rows)
        if 'insertErrors' in response:
          raise Exception('insertAll failed: %s' % response['insertErrors'])

        time.sleep(post_interval)  # Streaming API is rate-limited
        rows = []
    return self

  def _init_job_from_response(self, response):
    """ Helper function to create a Job instance from a response. """
    job = None
    if response and 'jobReference' in response:
      job = _BQJob(self._api, job_id=response['jobReference']['jobId'])
    return job

  def extract_async(self, destination, format='csv', csv_delimiter=',', csv_header=True,
                    compress=False):
    """Runs a job to export the table to GCS.

    Args:
      destination: the destination URI(s). Can be a single URI or a list.
      format: the format to use for the exported data; one of 'csv', 'json', or 'avro'
          (default 'csv').
      csv_delimiter: for CSV exports, the field delimiter to use. Defaults to ','
      csv_header: for CSV exports, whether to include an initial header line. Default true.
      compress whether to compress the data on export. Compression is not supported for
          AVRO format. Defaults to False.
    Returns:
      A Job object for the export Job if it was started successfully; else None.
    """
    try:
      response = self._api.table_extract(self._name_parts, destination, format, compress,
                                         csv_delimiter, csv_header)
      return self._init_job_from_response(response)
    except Exception as e:
      raise _JobError(location=_format_exc(), message=e.message, reason=str(type(e)))

  def extract(self, destination, format='csv', csv_delimiter=',', csv_header=True, compress=False):
    """Exports the table to GCS; blocks until complete.

    Args:
      destination: the destination URI(s). Can be a single URI or a list.
      format: the format to use for the exported data; one of 'csv', 'json', or 'avro'
          (default 'csv').
      csv_delimiter: for CSV exports, the field delimiter to use. Defaults to ','
      csv_header: for CSV exports, whether to include an initial header line. Default true.
      compress whether to compress the data on export. Compression is not supported for
          AVRO format. Defaults to False.
    Returns:
      A Job object for the export Job if it was started successfully; else None.
    """
    format = format.upper()
    if format == 'JSON':
      format = 'NEWLINE_DELIMITED_JSON'
    job = self.extract_async(destination, format=format, csv_delimiter=csv_delimiter,
                             csv_header=csv_header, compress=compress)
    if job is not None:
      job.wait()
    return job

  def load_async(self, source, append=False, overwrite=False, create=False, source_format='CSV',
                 field_delimiter=',', allow_jagged_rows=False, allow_quoted_newlines=False,
                 encoding='UTF-8', ignore_unknown_values=False, max_bad_records=0, quote='"',
                 skip_leading_rows=0):
    """ Start loading the table from GCS and return a Future.

    Args:
      source: the URL of the source bucket(s). Can include wildcards.
      append: if True append onto existing table contents.
      overwrite: if True overwrite existing table contents.
      create: if True attempt to create the Table if needed, inferring the schema.
      source_format: the format of the data; default 'CSV'. Other options are DATASTORE_BACKUP
          or NEWLINE_DELIMITED_JSON.
      field_delimiter: The separator for fields in a CSV file. BigQuery converts the string to
          ISO-8859-1 encoding, and then uses the first byte of the encoded string to split the data
          as raw binary (default ',').
      allow_jagged_rows: If True, accept rows in CSV files that are missing trailing optional
          columns; the missing values are treated as nulls (default False).
      allow_quoted_newlines: If True, allow quoted data sections in CSV files that contain newline
          characters (default False).
      encoding: The character encoding of the data, either 'UTF-8' (the default) or 'ISO-8859-1'.
      ignore_unknown_values: If True, accept rows that contain values that do not match the schema;
          the unknown values are ignored (default False).
      max_bad_records The maximum number of bad records that are allowed (and ignored) before
          returning an 'invalid' error in the Job result (default 0).
      quote: The value used to quote data sections in a CSV file; default '"'. If your data does
          not contain quoted sections, set the property value to an empty string. If your data
          contains quoted newline characters, you must also enable allow_quoted_newlines.
      skip_leading_rows: A number of rows at the top of a CSV file to skip (default 0).

    Returns:
      A Future that returns a Job object for the load Job if it was started successfully or
      None if not.
    """
    response = self._api.jobs_insert_load(source, self._name_parts,
                                          append=append,
                                          overwrite=overwrite,
                                          create=create,
                                          source_format=source_format,
                                          field_delimiter=field_delimiter,
                                          allow_jagged_rows=allow_jagged_rows,
                                          allow_quoted_newlines=allow_quoted_newlines,
                                          encoding=encoding,
                                          ignore_unknown_values=ignore_unknown_values,
                                          max_bad_records=max_bad_records,
                                          quote=quote,
                                          skip_leading_rows=skip_leading_rows)
    return self._init_job_from_response(response)

  def load(self, source, append=False, overwrite=False, create=False,
           source_format='CSV', field_delimiter=',',
           allow_jagged_rows=False, allow_quoted_newlines=False, encoding='UTF-8',
           ignore_unknown_values=False, max_bad_records=0, quote='"', skip_leading_rows=0):
    """ Load the table from GCS.

    Args:
      source: the URL of the source bucket(s). Can include wildcards.
      append: if True append onto existing table contents.
      overwrite: if True overwrite existing table contents.
      create: if True attempt to create the Table if needed, inferring the schema.
      source_format: the format of the data; default 'CSV'. Other options are DATASTORE_BACKUP
          or NEWLINE_DELIMITED_JSON.
      field_delimiter: The separator for fields in a CSV file. BigQuery converts the string to
          ISO-8859-1 encoding, and then uses the first byte of the encoded string to split the data
          as raw binary (default ',').
      allow_jagged_rows: If True, accept rows in CSV files that are missing trailing optional
          columns; the missing values are treated as nulls (default False).
      allow_quoted_newlines: If True, allow quoted data sections in CSV files that contain newline
          characters (default False).
      encoding: The character encoding of the data, either 'UTF-8' (the default) or 'ISO-8859-1'.
      ignore_unknown_values: If True, accept rows that contain values that do not match the schema;
          the unknown values are ignored (default False).
      max_bad_records The maximum number of bad records that are allowed (and ignored) before
          returning an 'invalid' error in the Job result (default 0).
      quote: The value used to quote data sections in a CSV file; default '"'. If your data does
          not contain quoted sections, set the property value to an empty string. If your data
          contains quoted newline characters, you must also enable allow_quoted_newlines.
      skip_leading_rows: A number of rows at the top of a CSV file to skip (default 0).

    Returns:
      A Job object for the load Job if it was started successfully; else None.
    """
    job = self.load_async(source,
                          append=append,
                          overwrite=overwrite,
                          create=create,
                          source_format=source_format,
                          field_delimiter=field_delimiter,
                          allow_jagged_rows=allow_jagged_rows,
                          allow_quoted_newlines=allow_quoted_newlines,
                          encoding=encoding,
                          ignore_unknown_values=ignore_unknown_values,
                          max_bad_records=max_bad_records,
                          quote=quote,
                          skip_leading_rows=skip_leading_rows)
    if job is not None:
      job.wait()
    return job

  def _get_row_fetcher(self, start_row=0, max_rows=None, page_size=_DEFAULT_PAGE_SIZE):
    """ Get a function that can retrieve a page of rows.

    The function returned is a closure so that it can have a signature suitable for use
    by Iterator.

    Args:
      start_row: the row to start fetching from; default 0.
      max_rows: the maximum number of rows to fetch (across all calls, not per-call). Default
          is None which means no limit.
      page_size: the maximum number of results to fetch per page; default 1024.
    Returns:
      A function that can be called repeatedly with a page token and running count, and that
      will return an array of rows and a next page token; when the returned page token is None
      the fetch is complete.
    """
    if not start_row:
      start_row = 0
    elif start_row < 0:  # We are measuring from the table end
      if self.length >= 0:
        start_row += self.length
      else:
        raise Exception('Cannot use negative indices for table of unknown length')

    schema = self.schema._bq_schema
    name_parts = self._name_parts

    def _retrieve_rows(page_token, count):

      page_rows = []
      if max_rows and count >= max_rows:
        page_token = None
      else:
        if max_rows and page_size > (max_rows - count):
          max_results = max_rows - count
        else:
          max_results = page_size

        if page_token:
          response = self._api.tabledata_list(name_parts, page_token=page_token,
                                              max_results=max_results)
        else:
          response = self._api.tabledata_list(name_parts, start_index=start_row,
                                              max_results=max_results)
        page_token = response['pageToken'] if 'pageToken' in response else None
        if 'rows' in response:
          page_rows = response['rows']

      rows = []
      for row_dict in page_rows:
        rows.append(_Parser.parse_row(schema, row_dict))

      return rows, page_token

    return _retrieve_rows

  def range(self, start_row=0, max_rows=None):
    """ Get an iterator to iterate through a set of table rows.

    Args:
      start_row: the row of the table at which to start the iteration (default 0)
      max_rows: an upper limit on the number of rows to iterate through (default None)

    Returns:
      A row iterator.
    """
    fetcher = self._get_row_fetcher(start_row=start_row, max_rows=max_rows)
    return iter(_Iterator(fetcher))

  def to_dataframe(self, start_row=0, max_rows=None):
    """ Exports the table to a Pandas dataframe.

    Args:
      start_row: the row of the table at which to start the export (default 0)
      max_rows: an upper limit on the number of rows to export (default None)
    Returns:
      A dataframe containing the table data.
    """
    fetcher = self._get_row_fetcher(start_row=start_row, max_rows=max_rows)
    count = 0
    page_token = None
    df = None
    while True:
      page_rows, page_token = fetcher(page_token, count)
      if len(page_rows):
        count += len(page_rows)
        if df is None:
          df = pd.DataFrame.from_dict(page_rows)
        else:
          df = df.append(page_rows, ignore_index=True)
      if not page_token:
        break

    # Need to reorder the dataframe to preserve column ordering
    ordered_fields = [field.name for field in self.schema]
    return df[ordered_fields]

  def to_file(self, path, start_row=0, max_rows=None, write_header=True, dialect=csv.excel):
    """Save the results to a local file in CSV format.

    Args:
      path: path on the local filesystem for the saved results.
      start_row: the row of the table at which to start the export (default 0)
      max_rows: an upper limit on the number of rows to export (default None)
      write_header: if true (the default), write column name header row at start of file
      dialect: the format to use for the output. By default, csv.excel. See
          https://docs.python.org/2/library/csv.html#csv-fmt-params for how to customize this.
    Raises:
      An Exception if the operation failed.
    """
    f = codecs.open(path, 'w', 'utf-8')
    fieldnames = []
    for column in self.schema:
      fieldnames.append(column.name)
    writer = csv.DictWriter(f, fieldnames=fieldnames, dialect=dialect)
    if write_header:
      writer.writeheader()
    for row in self.range(start_row, max_rows):
      writer.writerow(row)
    f.close()

  @async_method
  def to_file_async(self, path, start_row=0, max_rows=None, write_header=True, dialect=csv.excel):
    """Start saving the results to a local file in CSV format and return a Job for completion.

    Args:
      path: path on the local filesystem for the saved results.
      start_row: the row of the table at which to start the export (default 0)
      max_rows: an upper limit on the number of rows to export (default None)
      write_header: if true (the default), write column name header row at start of file
      dialect: the format to use for the output. By default, csv.excel. See
          https://docs.python.org/2/library/csv.html#csv-fmt-params for how to customize this.
    Returns:
      A Job for the async save operation.
    Raises:
      An Exception if the operation failed.
    """
    self.to_file(path, start_row=start_row, max_rows=max_rows, write_header=write_header,
                 dialect=dialect)

  @property
  def schema(self):
    """Retrieves the schema of the table.

    Returns:
      A Schema object containing a list of schema fields and associated metadata.
    Raises
      Exception if the request could not be executed or the response was malformed.
    """
    try:
      self._load_info()
      return Schema(definition=self._info['schema']['fields'])
    except KeyError:
      raise Exception('Unexpected table response: missing schema')

  def update(self, friendly_name=None, description=None, expiry=None, schema=None):
    """ Selectively updates Table information.

    Args:
      friendly_name: if not None, the new friendly name.
      description: if not None, the new description.
      expiry: if not None, the new expiry time, either as a DateTime or milliseconds since epoch.
      schema: if not None, the new schema: either a list of dictionaries or a Schema.

    Returns:
    """
    self._load_info()
    if friendly_name is not None:
      self._info['friendly_name'] = friendly_name
    if description is not None:
      self._info['description'] = description
    if expiry is not None:
      if isinstance(expiry, datetime):
        expiry = time.mktime(expiry.timetuple()) * 1000
      self._info['expiry'] = expiry
    if schema is not None:
      if isinstance(schema, Schema):
        schema = schema._bq_schema
      self._info['schema'] = {'fields': schema}
    try:
      self._api.table_update(self._name_parts, self._info)
    except Exception:
      # The cached metadata is out of sync now; abandon it.
      self._info = None

  def _repr_sql_(self, args=None):
    """Returns a representation of the table for embedding into a SQL statement.

    Returns:
      A formatted table name for use within SQL statements.
    """
    return '[' + self._full_name + ']'

  def __repr__(self):
    """Returns a representation for the table for showing in the notebook.
    """
    return ''

  def __str__(self):
    """Returns a string representation of the table using its specified name.

    Returns:
      The string representation of this object.
    """
    return self.full_name

  @property
  def length(self):
    """ Get the length of the table (number of rows). We don't use __len__ as this may
        return -1 for 'unknown'.
    """
    return self.metadata.rows

  def __iter__(self):
    """ Get an iterator for the table.
    """
    return self.range(start_row=0)

  def __getitem__(self, item):
    """ Get an item or a slice of items from the table. This uses a small cache
        to reduce the number of calls to tabledata.list.

        Note: this is a useful function to have, and supports some current usage like
        query.results()[0], but should be used with care.
    """
    if isinstance(item, slice):
      # Just treat this as a set of calls to __getitem__(int)
      result = []
      i = item.start
      step = item.step if item.step else 1
      while i < item.stop:
        result.append(self[i])
        i += step
      return result

    # Handle the integer index case.
    if item < 0:
      if self.length >= 0:
        item += self.length
      else:
        raise Exception('Cannot use negative indices for table of unknown length')

    if not self._cached_page \
        or self._cached_page_index > item \
            or self._cached_page_index + len(self._cached_page) <= item:
      # cache a new page. To get the start row we round to the nearest multiple of the page
      # size.
      first = int(math.floor(item / self._DEFAULT_PAGE_SIZE)) * self._DEFAULT_PAGE_SIZE
      count = self._DEFAULT_PAGE_SIZE

      if self.length >= 0:
        remaining = self.length - first
        if count > remaining:
          count = remaining

      fetcher = self._get_row_fetcher(start_row=first, max_rows=count, page_size=count)
      self._cached_page_index = first
      self._cached_page, _ = fetcher(None, 0)

    return self._cached_page[item - self._cached_page_index]

  @staticmethod
  def _convert_decorator_time(when):
    if isinstance(when, datetime):
      value = 1000 * (when - datetime.utcfromtimestamp(0)).total_seconds()
    elif isinstance(when, timedelta):
      value = when.total_seconds() * 1000
      if value > 0:
        raise Exception("Invalid snapshot relative when argument: %s" % str(when))
    else:
      raise Exception("Invalid snapshot when argument type: %s" % str(when))

    if value < -Table._MSEC_PER_WEEK:
      raise Exception("Invalid snapshot relative when argument: must be within 7 days: %s"
                      % str(when))

    if value > 0:
      now = 1000 * (datetime.utcnow() - datetime.utcfromtimestamp(0)).total_seconds()
      # Check that an abs value is not more than 7 days in the past and is
      # not in the future
      if not ((now - Table._MSEC_PER_WEEK) < value < now):
        raise Exception("Invalid snapshot absolute when argument: %s" % str(when))

    return int(value)

  def snapshot(self, at):
    """ Return a new Table which is a snapshot of this table at the specified time.

    Args:
      at: the time of the snapshot. This can be a Python datetime (absolute) or timedelta
          (relative to current time). The result must be after the table was created and no more
          than seven days in the past. Passing None will get a reference the oldest snapshot.

          Note that using a datetime will get a snapshot at an absolute point in time, while
          a timedelta will provide a varying snapshot; any queries issued against such a Table
          will be done against a snapshot that has an age relative to the execution time of the
          query.

    Returns:
      A new Table object referencing the snapshot.

    Raises:
      An exception if this Table is already decorated, or if the time specified is invalid.
    """
    if self._name_parts.decorator != '':
      raise Exception("Cannot use snapshot() on an already decorated table")

    value = Table._convert_decorator_time(at)
    return Table(self._api, "%s@%s" % (self.full_name, str(value)))

  def window(self, begin, end=None):
    """ Return a new Table limited to the rows added to this Table during the specified time range.

    Args:
      begin: the start time of the window. This can be a Python datetime (absolute) or timedelta
          (relative to current time). The result must be after the table was created and no more
          than seven days in the past.

          Note that using a relative value will provide a varying snapshot, not a fixed
          snapshot; any queries issued against such a Table will be done against a snapshot
          that has an age relative to the execution time of the query.

      end: the end time of the snapshot; if None, then the current time is used. The types and
          interpretation of values is as for start.

    Returns:
      A new Table object referencing the window.

    Raises:
      An exception if this Table is already decorated, or if the time specified is invalid.
    """
    if self._name_parts.decorator != '':
      raise Exception("Cannot use window() on an already decorated table")

    start = Table._convert_decorator_time(begin)
    if end is None:
      if isinstance(begin, timedelta):
        end = timedelta(0)
      else:
        end = datetime.utcnow()
    stop = Table._convert_decorator_time(end)

    # Both values must have the same sign
    if (start > 0 >= stop) or (stop > 0 >= start):
      raise Exception("window: Between arguments must both be absolute or relative: %s, %s" %
                      (str(begin), str(end)))

    # start must be less than stop
    if start > stop:
      raise Exception("window: Between arguments: begin must be before end: %s, %s" %
                      (str(begin), str(end)))

    return Table(self._api, "%s@%s-%s" % (self.full_name, str(start), str(stop)))

from ._query import Query as _Query