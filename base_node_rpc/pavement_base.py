from paver.easy import task, needs, path, sh, cmdopts, options
import base_node_rpc


@task
def generate_rpc_buffer_header():
    import arduino_rpc.rpc_data_frame as rpc_df

    output_dir = path(options.PROPERTIES['name']).joinpath('Arduino', options.PROPERTIES['name'])
    rpc_df.generate_rpc_buffer_header(output_dir)


@task
def generate_command_processor_header():
    from arduino_rpc.code_gen import write_code
    from arduino_rpc.rpc_data_frame import get_c_header_code

    sketch_dir = path(options.PROPERTIES['name']).joinpath('Arduino', options.PROPERTIES['name'])
    lib_dir = base_node_rpc.get_lib_directory()
    input_classes = ['BaseNode', 'Node']
    input_headers = [lib_dir.joinpath('BaseNode.h'),
                     sketch_dir.joinpath('Node.h')]

    output_header = path(options.PROPERTIES['name']).joinpath('Arduino', options.PROPERTIES['name'],
                                                  'NodeCommandProcessor.h')
    extra_header = '\n'.join(['#define BASE_NODE__%s  ("%s")' % (k.upper(), v)
                              for k, v in options.PROPERTIES.iteritems()])
    f_get_code = lambda *args_: get_c_header_code(*(args_ +
                                                    (options.PROPERTIES['name'], )),
                                                  extra_header=extra_header)

    write_code(input_headers, input_classes, output_header, f_get_code)


@task
def generate_python_code():
    from arduino_rpc.code_gen import write_code
    from arduino_rpc.rpc_data_frame import get_python_code

    sketch_dir = path(options.PROPERTIES['name']).joinpath('Arduino', options.PROPERTIES['name'])
    lib_dir = base_node_rpc.get_lib_directory()
    output_file = path(options.PROPERTIES['name']).joinpath('node.py')
    input_classes = ['BaseNode', 'Node']
    input_headers = [lib_dir.joinpath('BaseNode.h'),
                     sketch_dir.joinpath('Node.h')]
    extra_header = ('from base_node_rpc.proxy import ProxyBase, I2cProxyMixin,'
                    ' I2cSoftProxyMixin')
    extra_footer = '''

class I2cProxy(I2cProxyMixin, Proxy):
    pass


class I2cSoftProxy(I2cSoftProxyMixin, Proxy):
    pass'''
    f_python_code = lambda *args: get_python_code(*args,
                                                  extra_header=extra_header,
                                                  extra_footer=extra_footer)
    write_code(input_headers, input_classes, output_file, f_python_code)


@task
@needs('generate_command_processor_header', 'generate_rpc_buffer_header')
@cmdopts([('sconsflags=', 'f', 'Flags to pass to SCons.'),
          ('boards=', 'b', 'Comma-separated list of board names to compile '
           'for (e.g., `uno`).')])
def build_firmware():
    scons_flags = getattr(options, 'sconsflags', '')
    boards = [b.strip() for b in getattr(options, 'boards', '').split(',')
              if b.strip()]
    if not boards:
        boards = options.DEFAULT_ARDUINO_BOARDS
    for board in boards:
        # Compile firmware once for each specified board.
        sh('scons %s ARDUINO_BOARD="%s"' % (scons_flags, board))


@task
@needs('generate_setup', 'minilib', 'build_firmware', 'generate_python_code',
       'setuptools.command.sdist')
def sdist():
    """Overrides sdist to make sure that our setup.py is generated."""
    pass
