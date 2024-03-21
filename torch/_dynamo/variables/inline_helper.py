import warnings

import torch.fx
import torch.fx.traceback as fx_traceback
import torch.utils._pytree as pytree
from torch.fx import Interpreter
from torch.nn.utils import stateless
from .constant import ConstantVariable


def dummy_user_function_to_inline_gm(gm, args):
    return gm(*args)


def dummy_user_function_to_inline_wrapped_gm(wrapped_gm, args, kwargs):
    return wrapped_gm(args, kwargs)


def dummy_accumulate_grad_(t1, t2):
    if t1.grad is None:
        t1.grad = t2
    else:
        t1.grad += t2


# copy from torch/_functorch/_aot_autograd/traced_function_transforms.py
# and remove the restriction that output has to be a tuple.
def create_functional_call(mod, params_spec, params_len, store_orig_mod=False):
    # Redundant with dynamo, but worth having in case this gets invoked elsewhere.
    # https://github.com/pytorch/pytorch/issues/103569

    def functional_call(*args, **kwargs):
        with stateless._reparametrize_module(
            mod, pytree.tree_unflatten(args[:params_len], params_spec)
        ):
            if isinstance(mod, torch.fx.GraphModule):
                with fx_traceback.preserve_node_meta(), warnings.catch_warnings():
                    warnings.filterwarnings(
                        "ignore", "Anomaly Detection has been enabled."
                    )
                    with torch.autograd.detect_anomaly(check_nan=False):
                        out = Interpreter(mod).run(*args[params_len:], **kwargs)
            else:
                out = mod(*args[params_len:], **kwargs)
        return out

    # Note [Preserving the nn module stack metadata during export non-strict mode]
    # This path is currently only used by the non-strict export flow,
    # where we cannot rely on dynamo to preserve nn stack metadata in our captured graph.
    # Instead, we stash the original user nn module here, and rely on `make_fx` to grab
    # this stashed module and use it to track nn module stack metadata
    if store_orig_mod and not hasattr(functional_call, "_orig_mod"):
        functional_call._orig_mod = mod  # type: ignore[attr-defined]

    return functional_call


function_key_to_fx = {}


def vt_to_fake_helper(vt, tx):
    from ..utils import get_fake_value

    proxy_ = vt.as_proxy()

    def proxy_to_fake_helper(p):
        if type(p) is torch.fx.proxy.Proxy:
            return get_fake_value(p.node, tx)
        elif type(p) is tuple:
            return tuple(map(proxy_to_fake_helper, p))
        else:
            # mostly handle scalar
            # check return type is a fake tensor
            assert type(p) != torch.fx.proxy.Proxy
            return p

    return proxy_to_fake_helper(proxy_)


def decompose_and_inline_function_with_makefx(tx, fn, args, kwargs, function_key=None):
    from functorch import make_fx

    from torch._dispatch.python import enable_python_dispatcher
    from torch._guards import detect_fake_mode
    from ..utils import get_fake_value
    from .base import MutableLocal
    from .builder import SourcelessBuilder
    from .dicts import ConstDictVariable
    from .lists import BaseListVariable

    if function_key is None or function_key not in function_key_to_fx:
        # convert he arguments from VariableTracker to fake tensors + constants again
        fake_value_args = []
        for arg in args:
            fake_value_args.append(vt_to_fake_helper(arg, tx))

        fake_value_kwargs = {}
        for key, value in kwargs.items():
            if type(value.as_proxy()) is torch.fx.proxy.Proxy:
                fake_value_kwargs[key] = get_fake_value(value.as_proxy().node, tx)
            else:
                # mostly handle tuple and scalar
                fake_value_kwargs[key] = value.as_proxy()

        # Wrap the function before calling make_fx to avoid make_fx modify the kwargs's key.
        def wrapper_fn(fn):
            def inner(arg, kwargs):
                return fn(*arg, **kwargs)

            return inner

        wrapped_fn = wrapper_fn(fn)
        fake_mode = detect_fake_mode(fake_value_args)

        with fake_mode:
            with enable_python_dispatcher():
                fx_g = make_fx(wrapped_fn, pre_dispatch=True)(
                    fake_value_args, fake_value_kwargs
                )

        if function_key is not None:
            function_key_to_fx[function_key] = fx_g
    else:
        fx_g = function_key_to_fx[function_key]

    # print("\nfx code")
    # this is a hack, we want to access `.code` here to trigger the `real_recompile`
    # in case this is `_lazy_graph_module`. This will aovid us trying to inline the
    # `_LazyGraphModule._lazy_forward`(in the skip list) below.
    temp = fx_g.code
    # print(fx_g.code)

    # now inline this fx graph and return the output
    user_fn_variable_with_kwargs = SourcelessBuilder.create(
        tx, dummy_user_function_to_inline_wrapped_gm
    )
    gm_variable = SourcelessBuilder.create(tx, fx_g)
    cls = BaseListVariable.cls_for(list)
    input_args_variable = cls(
        args,
        mutable_local=MutableLocal(),
    )

    # kwarg's key needs to be turn into VariableTracker before passing
    # to ConstDictVariable.
    updated_kwargs = {}
    for k, v in kwargs.items():
        updated_kwargs[ConstantVariable.create(k)] = v

    input_kwargs_variable = ConstDictVariable(
        updated_kwargs,
        dict,
        mutable_local=MutableLocal(),
    )
    res = tx.inline_user_function_return(
        user_fn_variable_with_kwargs,
        (gm_variable, input_args_variable, input_kwargs_variable),
        {},
    )
    return res
