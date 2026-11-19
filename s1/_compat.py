"""Re-export a moved module without changing its public names."""


def export_module(module_name, namespace):
    import importlib

    impl = importlib.import_module(module_name)
    for key, value in vars(impl).items():
        if key.startswith("__"):
            continue
        namespace[key] = value
    return impl
