
def classFactory(iface):
    from .ml_generator_plugin import MLGeneratorPlugin
    return MLGeneratorPlugin(iface)
