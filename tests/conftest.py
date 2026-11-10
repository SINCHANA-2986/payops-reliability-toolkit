import importlib

from prometheus_client import REGISTRY


def reload_app():
    """Reload app.main with fresh env config. Its metrics must be unregistered first or the
    default Prometheus registry raises DuplicateTimeseries."""
    import app.main as m  # first import registers metrics if not yet loaded
    for collector in list(REGISTRY._collector_to_names):
        names = REGISTRY._collector_to_names.get(collector, [])
        if any(n.startswith("payops_") for n in names):
            REGISTRY.unregister(collector)
    return importlib.reload(m)
