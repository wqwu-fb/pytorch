import collections
import copyreg
import io
import pickle
import sys
import threading
import traceback
import types
from enum import Enum

import torch
import torch.distributed as dist
from torch._C._distributed_rpc import _get_current_rpc_agent


# Thread local tensor tables to store tensors while pickling torch.Tensor
# objects
_thread_local_tensor_tables = threading.local()
_pickler = pickle.Pickler
_unpickler = pickle.Unpickler


class RPCExecMode(Enum):
    SYNC = "sync"
    ASYNC = "async"
    ASYNC_JIT = "async_jit"
    REMOTE = "remote"


class _InternalRPCPickler:
    r"""
    This class provides serialize() and deserialize() interfaces to serialize
    data to be "binary string + tensor table" format
    So for RPC python UDF function and args, non tensor data will be serialized
    into regular binary string, tensor data will be put into thread local tensor
    tables, this serialization format is consistent with builtin operator and args
    using JIT pickler. This format will make tensor handling in C++ much easier,
    e.g. attach tensor to distributed autograd graph in C++
    """

    def __init__(self):
        # Ignore type error because dispatch_table is defined in third-party package
        self._dispatch_table = copyreg.dispatch_table.copy()  # type: ignore[attr-defined]
        self._dispatch_table[torch.Tensor] = self._tensor_reducer

    @classmethod
    def _tensor_receiver(cls, tensor_index):
        global _thread_local_tensor_tables
        return _thread_local_tensor_tables.recv_tables[tensor_index]

    def _tensor_reducer(self, tensor):
        global _thread_local_tensor_tables
        _thread_local_tensor_tables.send_tables.append(tensor)
        tensor_index = len(_thread_local_tensor_tables.send_tables) - 1
        return (_InternalRPCPickler._tensor_receiver, (tensor_index,))

    @classmethod
    def _py_rref_receiver(cls, rref_fork_data):
        return dist.rpc.PyRRef._deserialize(rref_fork_data)

    def _py_rref_reducer(self, py_rref):
        rref_fork_data = py_rref._serialize()
        return (_InternalRPCPickler._py_rref_receiver, (rref_fork_data,))

    def _rref_reducer(self, rref):
        return self._py_rref_reducer(rref)

    @classmethod
    def _script_module_receiver(cls, script_module_serialized):
        """
        Given a serialized representation of a ScriptModule created with torch.jit.save,
        loads and returns the ScriptModule.
        """
        f = io.BytesIO(script_module_serialized)
        m = torch.jit.load(f)
        return m

    def _script_module_reducer(self, script_module):
        """
        Serializes a ScriptModule.
        """
        f = io.BytesIO()
        torch.jit.save(script_module, f)
        return (_InternalRPCPickler._script_module_receiver, (f.getvalue(),))


    @classmethod
    def _recursive_script_module_receiver(cls, recursive_script_module_serialized):
        """
        Given a serialized representation of a RecursiveScriptModule created with torch.jit.save,
        loads and returns the RecursiveScriptModule.
        """
        f = io.BytesIO(script_module_serialized)
        m = torch.jit.load(f)
        return m

    def _recursive_script_module_reducer(self, recursive_script_module):
        """
        Serializes a RecursiveScriptModule.
        """
        # FIXME: RRef should be pickled separately.
        f = io.BytesIO()
        torch.jit.save(recursive_script_module, f)
        return (_InternalRPCPickler._recursive_script_module_receiver, (f.getvalue(),))

    @classmethod
    def _remote_module_receiver(
        cls,
        on,
        device,
        is_device_map_set,
        is_scriptable,
        generated_methods,
        module_rref_fork_data,
    ):
        m = object.__new__(dist.nn.RemoteModule)  # type: ignore[attr-defined]
        m.on = on
        m.device = device
        m.is_device_map_set = is_device_map_set
        m.is_scriptable = is_scriptable
        m.generated_methods = generated_methods
        # Unpickling the attribute `module_rref` must invoke RRef's `_deserialize()` method.
        m.module_rref = dist.rpc.PyRRef._deserialize(module_rref_fork_data)

        # Install generated methods when unpickled.
        for method in generated_methods:
            method_name = method.__name__
            method = torch.jit.export(method)
            setattr(m, method_name, types.MethodType(method, m))

        return m

    def _remote_module_reducer(self, remote_module):
        pickled_attrs = {}
        for k, v in remote_module.__dict__.items():
            # Pickling the attribute `module_rref` must invoke RRef's `_serialize()` method.
            if k == "module_rref":
                pickled_attrs[k] = v._serialize()
            elif k in dist.nn._REMOTE_MODULE_PICKLED_ATTRIBUTES:  # type: ignore[attr-defined]
                pickled_attrs[k] = v
            # Check if unpickled attributes are all in _REMOTE_MODULE_ATTRIBUTES_IGNORE_FOR_PICKLING.
            elif k not in dist.nn._REMOTE_MODULE_ATTRIBUTES_IGNORE_FOR_PICKLING:  # type: ignore[attr-defined]
                print(
                    "The new attribute ``{}`` of RemoteModule is ignored during RPC pickling. "
                    "To pickle this attribute, it must be either in ``_REMOTE_MODULE_PICKLED_ATTRIBUTES`` or "
                    "``_REMOTE_MODULE_ATTRIBUTES_IGNORE_FOR_PICKLING``.".format(k),
                    file=sys.stderr,
                )

        return (
            _InternalRPCPickler._remote_module_receiver,
            tuple(pickled_attrs.values()),
        )

    def serialize(self, obj):
        r"""
        Serialize non tensor data into binary string, tensor data into
        tensor table
        """
        f = io.BytesIO()
        p = _pickler(f)
        p.dispatch_table = self._dispatch_table

        # rpc api could accept user picklers inheriting from _InternalRPCPickler to serialize rref,
        # user picklers could have different initialization function from _InternalRPCPickler,
        # but all the user picklers should call serialize() and use _rref_reducer to pickle rref
        # in python. also, when _internal_rpc_pickler is imported to rpc/api.py, rpc.RRef is not
        # compiled yet, it is not good place to acces rpc.RRef inside _InternalRPCPickler constructor,
        # so puting rref's dispatch table here
        #
        # The return value of a `rpc.remote(..)` call is type of `rpc.PyRRef`.
        # The deserialized RRef object on an RPC receiver side is type of `rpc.PyRRef`.
        # Ignore type error because dispatch_table is defined in third-party package
        p.dispatch_table[dist.rpc.PyRRef] = self._py_rref_reducer  # type: ignore[index]
        # An RRef created locally by RRef Python constructor is type of `rpc.RRef`.
        # Ignore type error because dispatch_table is defined in third-party package
        p.dispatch_table[dist.rpc.RRef] = self._rref_reducer  # type: ignore[index]
        # Ignore type error because dispatch_table is defined in third-party package
        p.dispatch_table[dist.nn.RemoteModule] = self._remote_module_reducer  # type: ignore[attr-defined, index]
        # Ignore type error because dispatch_table is defined in third-party package
        p.dispatch_table[torch.jit.ScriptModule] = self._script_module_reducer  # type: ignore[index]
        # Ignore type error because dispatch_table is defined in third-party package
        p.dispatch_table[torch.jit.RecursiveScriptModule] = self._recursive_script_module_reducer  # type: ignore[index]

        # save _thread_local_tensor_tables.send_tables if it is in nested call
        global _thread_local_tensor_tables
        if hasattr(_thread_local_tensor_tables, "send_tables"):
            old_send_tables = _thread_local_tensor_tables.send_tables
        else:
            old_send_tables = None
        _thread_local_tensor_tables.send_tables = []

        p.dump(obj)

        # restore _thread_local_tensor_tables.send_tables if return
        # from nested call, otherwise clean up the table
        tensors = _thread_local_tensor_tables.send_tables
        if old_send_tables is not None:
            _thread_local_tensor_tables.send_tables = old_send_tables
        else:
            del _thread_local_tensor_tables.send_tables

        return (f.getvalue(), tensors)

    def deserialize(self, binary_data, tensor_table):
        r"""
        Deserilize binary string + tensor table to original obj
        """
        # save _thread_local_tensor_tables.recv_tables if it is in nested call
        global _thread_local_tensor_tables
        if hasattr(_thread_local_tensor_tables, "recv_tables"):
            old_recv_tables = _thread_local_tensor_tables.recv_tables
        else:
            old_recv_tables = None
        _thread_local_tensor_tables.recv_tables = tensor_table

        try:
            unpickler = _unpickler(io.BytesIO(binary_data))
            ret = unpickler.load()
        except AttributeError as e:
            # Occurs when function is not found on module/class during
            # unpickling.
            except_str = (
                str(e)
                + """ Default RPC pickler does not serialize
            function code. Ensure that UDFs are defined on both caller and
            callee modules."""
            )
            ret = AttributeError(except_str)

        # restore _thread_local_tensor_tables.recv_tables if return
        # from nested call, otherwise clean up the table
        if old_recv_tables is not None:
            _thread_local_tensor_tables.recv_tables = old_recv_tables
        else:
            del _thread_local_tensor_tables.recv_tables

        return ret


# Create _internal_rpc_pickler only once to initialize _dispatch_table only once
_internal_rpc_pickler = _InternalRPCPickler()


def serialize(obj):
    return _internal_rpc_pickler.serialize(obj)


def deserialize(binary_data, tensor_table):
    return _internal_rpc_pickler.deserialize(binary_data, tensor_table)


def _run_function(python_udf):
    r"""
    This function is exclusively called from C++.
    See ``torch/csrc/distributed/rpc/python_rpc_handler.cpp``.

    Runs a Python UDF and returns its return value.
    Wraps any exception in ``RemoteException`` if the function raises.
    """
    try:
        if isinstance(python_udf, AttributeError):
            raise python_udf
        result = python_udf.func(*python_udf.args, **python_udf.kwargs)
    except Exception as e:
        # except str = exception info + traceback string
        except_str = (
            f"On {_get_current_rpc_agent().get_worker_info()}:\n"
            f"{repr(e)}\n{traceback.format_exc()}"
        )
        print(except_str, file=sys.stderr)
        result = RemoteException(except_str, type(e))
    return result


def _handle_exception(result):
    if isinstance(result, RemoteException):
        raise result.exception_type(result.msg.encode("utf-8").decode("unicode_escape"))


def _build_rpc_profiling_key(
    exec_type, func_name, current_worker_name, dst_worker_name
):
    """
    Builds the key that RPC calls are profiled with using the autograd profiler.
    This will be the name of the corresponding Event recorded in the profiler.

    Args:
        exec_type (RPCExecMode): Type of RPC/RRef call
        func_name (str): Name of function being profiled.
        current_worker_name (str): Name of current worker.
        dst_worker_name (str): Name of the destination worker.

    Returns:
        String representing profiling key
    """
    profile_key = "rpc_{rpc_type}#{func_name}({current_worker} -> {dst_worker})".format(
        rpc_type=exec_type.value,
        func_name=func_name,
        current_worker=current_worker_name,
        dst_worker=dst_worker_name,
    )
    return profile_key


def _start_record_function(exec_type, func_name, current_worker_name, dest_worker_name):
    """
    This function should be called from RPC/RRef functions to create a
    RecordFunction object for profiling. This function also runs the before
    callbacks that start the profiling, though the user is responsible for
    running the appropriate callbacks when the function to be profiled finishes.

    Args:
        exec_type (RPCExecMode): Type of RPC/RRef call
        func_name (str): Name of function being profiled.
        current_worker_name (str): Name of current worker.
        dest_worker_name (str): Name of the destination worker.

    Returns:
        An instance of `torch.autograd._RecordFunction`.
    """
    assert torch.autograd._profiler_enabled(), "Autograd profiler should be enabled."
    profile_key = "rpc_{}#{}({} -> {})".format(
        exec_type.value, str(func_name), current_worker_name, dest_worker_name
    )
    rf = torch.autograd._RecordFunction()  # type: ignore[attr-defined]
    torch.autograd._run_before_callbacks(rf, profile_key)  # type: ignore[attr-defined]
    return rf


PythonUDF = collections.namedtuple("PythonUDF", ["func", "args", "kwargs"])
RemoteException = collections.namedtuple("RemoteException", ["msg", "exception_type"])
