# coding: utf-8
from types import ModuleType
from typing import Optional, Dict, Union, Tuple, List

import jinja2
import warnings

import nanopb_helpers as npb
import platformio_helpers as pioh

from datetime import datetime
from importlib import import_module

from path_helpers import path
from arduino_rpc.helpers import verify_library_directory

from . import get_lib_directory
from .protobuf import get_handler_validator_class_code, write_handler_validator_header

from arduino_rpc.code_gen import write_code, C_GENERATED_WARNING_MESSAGE, PYTHON_GENERATED_WARNING_MESSAGE
from arduino_rpc.rpc_data_frame import (get_c_commands_header_code,
                                        get_c_command_processor_header_code, get_python_code)
from clang_helpers.data_frame import underscore_to_camelcase

DEFAULT_BASE_CLASSES = ['BaseNodeSerialHandler', 'BaseNodeEeprom',
                        'BaseNodeI2c', 'BaseNodeI2cHandler<Handler>',
                        'BaseNodeSpi']

DEFAULT_METHODS_FILTER = lambda df: df[~(df.method_name.isin(['get_config_fields', 'get_state_fields']))].copy()
DEFAULT_POINTER_BITWIDTH = 16

STDINT_STUB_PATH = path(__file__).parent.joinpath('StdIntStub').realpath()
C_ARRAY_DEFS_PATH = pioh.conda_arduino_include_path().joinpath('CArrayDefs')


def generate_library_main_header(lib_options: Dict) -> None:
    """
    Generate an (empty) header file which may be included in the Arduino sketch
    to trigger inclusion of the rest of the library.
    """
    module_name = lib_options['module_name']

    library_dir = verify_library_directory(lib_options)
    library_header = library_dir.joinpath('src', f'{library_dir.name}.h')
    library_header.parent.makedirs(exist_ok=True)

    header = '#ifndef ___{module_name_upper}__H___\n' \
             '#define ___{module_name_upper}__H___\n' \
             '#endif  // #ifndef ___{module_name_upper}__H___'.strip(). \
        format(module_name_upper=module_name.upper())

    library_header.write_text(header)
    print(f"Generated main header '{library_header.name}' > {library_header}")


def generate_protobuf_c_code(lib_options: Dict) -> None:
    """
    For each Protocol Buffer definition (i.e., `*.proto`) in the sketch
    directory, use the nano protocol buffer compiler to generate C code for the
    corresponding protobuf message structure(s).
    """
    module_name = lib_options['module_name']
    sketch_dir = lib_options['rpc_module'].get_sketch_directory()

    for proto_path in sketch_dir.abspath().files('*.proto'):
        proto_name = proto_path.namebase
        options_path = proto_path.with_suffix('.options')
        if options_path.isfile():
            kwargs = {'options_file': options_path}
        else:
            kwargs = {}

        project_lib_dir = verify_library_directory(lib_options)

        arduino_src_dir = project_lib_dir.joinpath('src', project_lib_dir.name)
        arduino_src_dir.makedirs(exist_ok=True)

        try:
            nano_pb_code = npb.compile_nanopb(proto_path, **kwargs)
        except Exception as e:
            print(e)
            raise e

        c_output_base = arduino_src_dir.joinpath(f'{proto_name}_pb')
        c_header_path = c_output_base.with_suffix('.h')
        c_output_path = c_output_base.with_suffix('.c')

        with open(c_output_path, 'w') as output:
            print(C_GENERATED_WARNING_MESSAGE.format(datetime.now()), file=output)
            output.write(nano_pb_code['source'].replace('{{ header_path }}', c_header_path.name))

        with open(c_header_path, 'w') as output:
            print(C_GENERATED_WARNING_MESSAGE.format(datetime.now()), file=output)
            output.write(nano_pb_code['header']
                         .replace(f'PB_{proto_name.upper()}_PB_H_INCLUDED',
                                  f'PB__{module_name.upper()}__{proto_name.upper()}_PB_H_INCLUDED'))

        print(f"Generated Protobuf C code\n"
              f"\t'{c_output_path.name}' > {c_output_path}\n"
              f"\t'{c_header_path.name}' > {c_header_path}")


def generate_protobuf_python_code(lib_options: Dict) -> None:
    module_name = lib_options['module_name']
    src_dir = path(lib_options['source_dir'])
    sketch_dir = lib_options['rpc_module'].get_sketch_directory()

    for proto_path in sketch_dir.abspath().files('*.proto'):
        proto_name = proto_path.namebase
        pb_code = npb.compile_pb(proto_path)
        output_path = src_dir.joinpath(module_name, f'{proto_name}.py').abspath()
        output_path.write_text(pb_code['python'])

        print(f"Generated Protobuf Python code '{output_path.name}' > {output_path}")


def get_base_classes_and_headers(lib_options: Dict, sketch_dir: path) -> Tuple[List[str], List[path]]:
    """
    Return ordered list of classes to scan for method discovery, along with a
    corresponding list of the header file where each class may be found.

     - Base classes refer to classes that are to be found in the
       `base-node-rpc` library directory.
     - rpc classes refer to classes found in the sketch directory.
    """
    module_name = lib_options['module_name']
    base_classes = lib_options.get('base_classes', DEFAULT_BASE_CLASSES)
    rpc_classes = lib_options.get('rpc_classes', [f'{module_name}::Node'])

    input_classes = ['BaseNode'] + base_classes + rpc_classes

    # Assume `base-node-rpc` has already been installed as a Conda package.
    base_node_lib_dir = pioh.conda_arduino_include_path().joinpath('BaseNodeRpc', 'src', 'BaseNodeRpc')
    if not base_node_lib_dir.isdir():
        # Library directory not found in Conda include paths since
        # `base-node-rpc` has **not** been installed as a Conda package.
        #
        # Assume running code from source directory.
        base_node_lib_dir = get_lib_directory().joinpath('BaseNodeRpc', 'src', 'BaseNodeRpc')
    input_headers = ([base_node_lib_dir.joinpath('BaseNode.h')] +
                     [base_node_lib_dir.joinpath(f"{c.split('<')[0]}.h")
                      for c in base_classes] + len(rpc_classes) * [sketch_dir.joinpath('Node.h')])

    return input_classes, input_headers


def generate_validate_header(lib_options: Dict, py_proto_module_name: str, sketch_dir: Union[str, path]) -> None:
    """
    If a package has a Protocol Buffer message class type with the specified
    message name defined, scan node base classes for callback methods related to
    the message type.

    For example, if the message name is `Config`, callbacks of the form
    `on_config_<field name>_changed` will be matched.

    The following callback signatures are supported:

        bool on_config_<field name>_changed()
        bool on_config_<field name>_changed(new_value)
        bool on_config_<field name>_changed(current_value, new_value)

    The corresponding field in the Protocol Buffer message will be set to the new
    value *only* if the callback returns `true`.
    """
    module_name = lib_options['module_name']
    c_protobuf_struct_name = underscore_to_camelcase(py_proto_module_name)

    try:
        mod = import_module(f'.{py_proto_module_name}', package=module_name)
    except ImportError:
        warnings.warn(f'ImportError: could not import {module_name}.{py_proto_module_name}')
        return

    lib_dir = get_lib_directory()

    if hasattr(mod, c_protobuf_struct_name):
        input_classes, input_headers = get_base_classes_and_headers(lib_options, sketch_dir)

        message_type = getattr(mod, c_protobuf_struct_name)

        # Add stub `stdint.h` header to includes path.
        args = ['-DSTDINT_STUB']
        include_paths = [STDINT_STUB_PATH, lib_dir.realpath(), C_ARRAY_DEFS_PATH]
        args += [f'-I{p}' for p in include_paths]

        validator_code = get_handler_validator_class_code(input_headers, input_classes, message_type, *args)

        output_path = path(sketch_dir).joinpath(f'{module_name}_{c_protobuf_struct_name.lower()}_validate.h')
        write_handler_validator_header(output_path, module_name, c_protobuf_struct_name.lower(), validator_code)


def generate_validate_headers(lib_options: Dict) -> None:
    """
    For each Protocol Buffer definition (i.e., `*.proto`) in the sketch
    directory, generate code to call corresponding validation methods (if any)
    present on the `Node` class.

    See `generate_validate_header` for more information.
    """
    sketch_dir = lib_options['rpc_module'].get_sketch_directory()

    for proto_path in sketch_dir.abspath().files('*.proto'):
        proto_name = proto_path.namebase
        print(f"[generate_validate_headers] Generate validation header for '{proto_name}'")
        generate_validate_header(lib_options, proto_name, sketch_dir)


def generate_command_processor_header(lib_options: Dict) -> None:
    """
    Generate the following headers in a project directory under
    `Arduino/library`:

     - `Commands.h`
     - `Properties.h`
     - `CommandProcessor.h`

    ## `Commands.h` ##

    This header defines the 8-bit code and request/response C structures
    associated with each command type.  This header can, for example, be
    included by other projects to make requests through i2c.

    ## `Properties.h` ##

    Contains property string define statements (e.g. `BASE_NODE__NAME`).

    ## `CommandProcessor.h` ##

    Contains the `CommandProcessor` C++ class for the project.

    ## `NodeCommandProcessor.h` ##

    This header is written to the sketch directory and simply includes the
    three library headers above.
    """
    module_name = lib_options['module_name']
    sketch_dir = lib_options['rpc_module'].get_sketch_directory()

    lib_dir = get_lib_directory()

    input_classes, input_headers = get_base_classes_and_headers(lib_options, sketch_dir)
    camel_name = underscore_to_camelcase(module_name)

    project_lib_dir = verify_library_directory(lib_options)
    arduino_src_dir = project_lib_dir.joinpath('src', project_lib_dir.name)
    arduino_src_dir.makedirs(exist_ok=True)

    with arduino_src_dir.joinpath('Properties.h').open('w') as output:
        print(C_GENERATED_WARNING_MESSAGE.format(datetime.now()), file=output)
        print(f'#ifndef ___{module_name.upper()}__PROPERTIES___', file=output)
        print(f'#define ___{module_name.upper()}__PROPERTIES___', file=output)
        print('', file=output)
        for key, v in lib_options['PROPERTIES'].items():
            print(f'#ifndef BASE_NODE__{key.upper()}', file=output)
            print(f'#define BASE_NODE__{key.upper()}  ("{v}")', file=output)
            print('#endif', file=output)
        print('', file=output)
        print('#endif', file=output)

    template_text = ('#ifndef ___{{ name.upper()  }}___\n'
                     '#define ___{{ name.upper()  }}___\n\n'
                     '#include "{{ camel_name }}/Properties.h"\n'
                     '#include "{{ camel_name }}/CommandProcessor.h"\n\n'
                     '#endif  // #ifndef ___{{ name.upper()  }}___')
    with sketch_dir.joinpath('NodeCommandProcessor.h').open('w') as output:
        template = jinja2.Template(template_text)
        print(C_GENERATED_WARNING_MESSAGE.format(datetime.now()), file=output)
        print(template.render(name=module_name, camel_name=camel_name), file=output)
        print('', file=output)

    print(f"Generated 'NodeCommandProcessor.h' > {sketch_dir.joinpath('NodeCommandProcessor.h')}")

    headers = {'Commands': get_c_commands_header_code,
               'CommandProcessor': get_c_command_processor_header_code}

    methods_filter = lib_options.get('methods_filter', DEFAULT_METHODS_FILTER)
    pointer_width = lib_options.get('pointer_width', DEFAULT_POINTER_BITWIDTH)

    for key, frame in headers.items():
        output_header = arduino_src_dir.joinpath(f'{key}.h')
        # Add stub `stdint.h` header to includes path.
        args = ['-DSTDINT_STUB']
        include_paths = [STDINT_STUB_PATH, lib_dir.realpath(), C_ARRAY_DEFS_PATH]
        args += [f'-I{p}' for p in include_paths]

        get_df_code = lambda *args_: ((C_GENERATED_WARNING_MESSAGE.format(datetime.now())) +
                                      frame(*(args_ + (module_name,)), pointer_width=pointer_width))

        write_code(input_headers, input_classes, output_header, get_df_code,
                   *args, methods_filter=methods_filter, pointer_width=pointer_width)

        print(f"Generated '{output_header.name}' > {output_header}")


def generate_rpc_buffer_header(lib_options: Dict) -> None:
    import arduino_rpc.rpc_data_frame as rpc_df
    sketch_dir = lib_options['rpc_module'].get_sketch_directory()
    rpc_df.generate_rpc_buffer_header(sketch_dir, source_dir=sketch_dir)


def generate_python_code(lib_options: Dict) -> None:
    module_name = lib_options['module_name']
    sketch_dir = lib_options['rpc_module'].get_sketch_directory()

    lib_dir = get_lib_directory()

    output_file = lib_options['rpc_module'].package_path().joinpath('node.py')
    input_classes, input_headers = get_base_classes_and_headers(lib_options, sketch_dir)

    extra_header = 'from base_node_rpc.proxy import ProxyBase, I2cProxyMixin, SerialProxyMixin'
    extra_footer = ('\nclass I2cProxy(I2cProxyMixin, Proxy):\n\tpass\n\n'
                    '\nclass SerialProxy(SerialProxyMixin, Proxy):\n\tpass')
    methods_filter = lib_options.get('methods_filter', DEFAULT_METHODS_FILTER)
    pointer_width = lib_options.get('pointer_width', DEFAULT_POINTER_BITWIDTH)

    python_code_gen = lambda *args_: ((PYTHON_GENERATED_WARNING_MESSAGE.format(datetime.now())) +
                                      get_python_code(*args_, extra_header=extra_header, extra_footer=extra_footer,
                                                      pointer_width=pointer_width))

    # Add stub `stdint.h` header to the 'include' path.
    args = ['-DSTDINT_STUB']
    include_paths = [STDINT_STUB_PATH, lib_dir.realpath(), C_ARRAY_DEFS_PATH]
    args += [f'-I{p}' for p in include_paths]

    write_code(input_headers, input_classes, output_file, python_code_gen,
               *args, methods_filter=methods_filter,
               pointer_width=pointer_width)

    print(f"Generated Python code '{output_file.name}' > {output_file}")


def build_arduino_library(lib_options: Dict, **kwargs) -> None:
    from arduino_rpc.helpers import build_arduino_library as build_arduino_library_
    build_arduino_library_(lib_options, **kwargs)


def generate_all_code(lib_options: Dict) -> None:
    """
    Generate all C++ (device) and Python (host) code, but do not compile device sketch.
    """
    top = f"{'#' * 80} Generating All Code {'#' * 80}"
    print(top)

    generate_library_main_header(lib_options)
    generate_protobuf_c_code(lib_options)
    generate_protobuf_python_code(lib_options)
    generate_validate_headers(lib_options)
    generate_command_processor_header(lib_options)
    generate_rpc_buffer_header(lib_options)
    generate_python_code(lib_options)

    print(f"{'#' * len(top)}")
