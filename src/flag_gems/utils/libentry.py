import inspect
import threading

import torch
import triton

DEVICE_COUNT = torch.xpu.device_count()


class LibEntry(triton.KernelInterface):
    def __init__(
        self,
        fn,
    ):
        self.fn = fn
        self.arg_names = fn.arg_names
        self.divisibility = 16
        self.kernel_cache = tuple(dict() for _ in range(DEVICE_COUNT))

        fn = self.fn
        while not isinstance(fn, triton.runtime.JITFunction):
            fn = fn.fn
        self.jit_function: triton.runtime.JITFunction = fn
        self.specialize_indices = [
            p.num
            for p in self.jit_function.params
            if not p.is_constexpr and not p.do_not_specialize
        ]
        self.do_not_specialize_indices = [
            p.num
            for p in self.jit_function.params
            if not p.is_constexpr and p.do_not_specialize
        ]
        self.lock = threading.Lock()

    def key(self, spec_args, dns_args, const_args):
        spec_key = [
            (arg.dtype, arg.data_ptr() % self.divisibility == 0)
            if hasattr(arg, "data_ptr")
            else (type(arg), arg)
            for arg in spec_args
        ]
        dns_key = [
            arg.dtype
            if hasattr(arg, "data_ptr")
            else type(arg)
            if not isinstance(arg, int)
            else "i32"
            if -(2**31) <= arg and arg <= 2**31 - 1
            else "u64"
            if 2**63 <= arg and arg <= 2**64 - 1
            else "i64"
            for arg in dns_args
        ]
        # const args passed by position
        return tuple(spec_key + dns_key + const_args)

    def run(self, *args, **kwargs):
        grid = kwargs["grid"]

        # collect all the arguments
        spec_args = []  # specialize arguments
        dns_args = []  # do not specialize arguments
        const_args = []  # constexpr arguments
        k_args = []  # kernel arguments
        for i, arg in enumerate(args):
            if i in self.specialize_indices:
                k_args.append(arg)
                spec_args.append(arg)
            elif i in self.do_not_specialize_indices:
                k_args.append(arg)
                dns_args.append(arg)
            else:
                const_args.append(arg)
        for p in self.jit_function.params[len(args) :]:
            if p.name in kwargs:
                val = kwargs[p.name]
            elif p.default is inspect._empty:
                continue
            else:
                val = p.default

            if p.is_constexpr:
                const_args.append(val)
            elif p.do_not_specialize:
                dns_args.append(val)
                k_args.append(val)
            else:
                spec_args.append(val)
                k_args.append(val)

        entry_key = self.key(spec_args, dns_args, const_args)
        device = torch.xpu.current_device()
        cache = self.kernel_cache[device]
        while entry_key not in cache:
            # NOTE: we serialize the first run of a jit function regardless of which device to run on
            # because Triton runtime is currently not threadsafe.
            with self.lock:
                if entry_key in cache:
                    break
                kernel = self.fn.run(*args, **kwargs)
                fn = self.fn
                # collect constexpr arguments for grid computation
                constexprs = {}
                while not isinstance(fn, triton.runtime.JITFunction):
                    if isinstance(fn, triton.runtime.Autotuner):
                        config = fn.best_config
                        constexprs["num_warps"] = config.num_warps
                        constexprs["num_stages"] = config.num_stages
                        constexprs["num_ctas"] = config.num_ctas
                        constexprs = {**constexprs, **config.kwargs}
                    elif isinstance(fn, triton.runtime.Heuristics):
                        for v, heur in fn.values.items():
                            constexprs[v] = heur(
                                {
                                    **dict(zip(fn.arg_names, args)),
                                    **kwargs,
                                    **constexprs,
                                }
                            )
                    else:
                        raise RuntimeError("Invalid Runtime Function")
                    fn = fn.fn
                for p in self.jit_function.params:
                    if p.is_constexpr and p.name not in constexprs:
                        constexprs[p.name] = p.default
                cache[entry_key] = (kernel, constexprs)
            return kernel, constexprs

        kernel, constexprs = cache[entry_key]

        if callable(grid):
            # collect all arguments to the grid fn，ie:
            # 1. args,
            # 2. kwargs,
            # 3. all all other captured arguments in CompiledKernel from Autotunner & Heuristics
            # when kwargs & captured args conflict, captured args have higher priority
            meta = {**dict(zip(self.arg_names, args)), **kwargs, **constexprs}
            grid = grid(meta)
        grid = grid + (1, 1)

        kernel[grid[0:3]](*k_args)
        return kernel, constexprs


def libentry():
    """
    Decorator for triton library entries.
    """

    def decorator(fn):
        return LibEntry(fn)

    return decorator
