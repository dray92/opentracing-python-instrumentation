# Copyright (c) 2015 Uber Technologies, Inc.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
from __future__ import absolute_import
import contextlib2
from opentracing_instrumentation import get_current_span
from ..local_span import func_span

# Utils for instrumenting DB API v2 compatible drivers.
# PEP-249 - https://www.python.org/dev/peps/pep-0249/

_COMMIT = 'commit'
_ROLLBACK = 'rollback'
NO_ARG = object()


def db_span(sql_statement,
            module_name,
            sql_parameters=None,
            connect_params=None,
            cursor_params=None):
    span = get_current_span()

    @contextlib2.contextmanager
    def empty_ctx_mgr():
        yield

    if span is None:
        return empty_ctx_mgr

    statement = sql_statement.strip()
    add_sql_tag = True
    if sql_statement == _COMMIT or sql_statement == _ROLLBACK:
        operation = sql_statement
        add_sql_tag = False
    else:
        space_idx = statement.find(' ')
        if space_idx == -1:
            operation = ''  # unrecognized format of the query
        else:
            operation = statement[0:space_idx]

    tags = dict()
    if add_sql_tag:
        tags['sql'] = statement
    if sql_parameters:
        tags['sql.params'] = str(sql_parameters)
    if connect_params:
        tags['sql.conn'] = str(connect_params)
    if cursor_params:
        tags['sql.cursor'] = str(cursor_params)

    return span.start_child(
        operation_name='%s:%s' % (module_name, operation),
        tags=tags
    )


class ConnectionFactory(object):
    """
    Wraps connect_func of the DB API v2 module by creating a wrapper object
    for the actual connection.
    """

    def __init__(self, connect_func, module_name, conn_wrapper_ctor=None):
        self._connect_func = connect_func
        self._module_name = module_name
        if hasattr(connect_func, '__name__'):
            self._connect_func_name = '%s:%s' % (module_name,
                                                 connect_func.__name__)
        else:
            self._connect_func_name = '%s:%s' % (module_name,
                                                 str(connect_func))
        self._wrapper_ctor = conn_wrapper_ctor \
            if conn_wrapper_ctor is not None else ConnectionWrapper

    def __call__(self, *args, **kwargs):
        safe_kwargs = kwargs
        if 'passwd' in safe_kwargs or 'password' in safe_kwargs:
            safe_kwargs = dict(kwargs)
            if 'passwd' in safe_kwargs:
                del safe_kwargs['passwd']
            if 'password' in safe_kwargs:
                del safe_kwargs['password']
        connect_params = (args, safe_kwargs) if args or safe_kwargs else None
        with func_span(self._connect_func_name):
            return self._wrapper_ctor(
                connection=self._connect_func(*args, **kwargs),
                module_name=self._module_name,
                connect_params=connect_params)


class ConnectionWrapper(object):
    def __init__(self, connection, module_name, connect_params):
        self._connection = connection
        self._module_name = module_name
        self._connect_params = connect_params
        object.__setattr__(self, 'close', connection.close)

    def cursor(self, *args, **kwargs):
        return CursorWrapper(
            cursor=self._connection.cursor(*args, **kwargs),
            module_name=self._module_name,
            connect_params=self._connect_params,
            cursor_params=(args, kwargs) if args or kwargs else None)

    def commit(self):
        with db_span(sql_statement=_COMMIT, module_name=self._module_name):
            return self._connection.commit()

    def rollback(self):
        with db_span(sql_statement=_ROLLBACK, module_name=self._module_name):
            return self._connection.rollback()


class ContextManagerConnectionWrapper(ConnectionWrapper):
    """
    Extends ConnectionWrapper by implementing `__enter__` and `__exit__`
    methods of the context manager API, for connections that can be used
    in as context managers to control the transactions, e.g.

    .. code-block:: python

        with MySQLdb.connect(...) as cursor:
            cursor.execute(...)
    """

    def __init__(self, connection, module_name, connect_params):
        super(ContextManagerConnectionWrapper, self).__init__(
            connection=connection,
            module_name=module_name,
            connect_params=connect_params
        )

    def __enter__(self):
        with func_span('%s:begin_transaction' % self._module_name):
            cursor = self._connection.__enter__()

        return CursorWrapper(cursor=cursor,
                             module_name=self._module_name,
                             connect_params=self._connect_params)

    def __exit__(self, exc, value, tb):
        outcome = _COMMIT if exc is None else _ROLLBACK
        with db_span(sql_statement=outcome, module_name=self._module_name):
            return self._connection.__exit__(exc, value, tb)


class CursorWrapper(object):
    def __init__(self, cursor, module_name,
                 connect_params=None, cursor_params=None):
        self._cursor = cursor
        self._module_name = module_name
        self._connect_params = connect_params
        self._cursor_params = cursor_params
        object.__setattr__(self, 'fetchone', cursor.fetchone)
        object.__setattr__(self, 'fetchmany', cursor.fetchmany)
        object.__setattr__(self, 'fetchall', cursor.fetchall)
        # We could also start a span to capture the life time of the cursor
        object.__setattr__(self, 'close', cursor.close)
        if hasattr(cursor, 'nextset'):
            object.__setattr__(self, 'nextset', cursor.nextset)
        if hasattr(cursor, 'setinputsizes'):
            object.__setattr__(self, 'setinputsizes', cursor.setinputsizes)
        if hasattr(cursor, 'setoutputsizes'):
            object.__setattr__(self, 'setoutputsizes', cursor.setoutputsizes)

    def execute(self, sql, params=NO_ARG):
        with db_span(sql_statement=sql,
                     sql_parameters=params if params is not NO_ARG else None,
                     module_name=self._module_name,
                     connect_params=self._connect_params,
                     cursor_params=self._cursor_params):
            if params is NO_ARG:
                return self._cursor.execute(sql)
            else:
                return self._cursor.execute(sql, params)

    def executemany(self, sql, seq_of_parameters):
        with db_span(sql_statement=sql, sql_parameters=seq_of_parameters,
                     module_name=self._module_name,
                     connect_params=self._connect_params,
                     cursor_params=self._cursor_params):
            return self._cursor.executemany(sql, seq_of_parameters)

    def callproc(self, proc_name, params=NO_ARG):
        with db_span(sql_statement='sproc:%s' % proc_name,
                     sql_parameters=params if params is not NO_ARG else None,
                     module_name=self._module_name,
                     connect_params=self._connect_params,
                     cursor_params=self._cursor_params):
            if params is NO_ARG:
                return self._cursor.callproc(proc_name)
            else:
                return self._cursor.callproc(proc_name, params)
