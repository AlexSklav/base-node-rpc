import warnings

import pandas as pd
from arduino_rpc.protobuf import (extract_callback_data,
                                  get_protobuf_fields_frame)
from arduino_rpc.code_gen import get_multilevel_method_sig_frame


def get_handler_sig_info_frame(header, class_, *args, **kwargs):
    '''
    Return `pandas.DataFrame` containing method signature information for methods
    matching the following pattern:

        bool on_<field ['__' field]*>_changed(...)

    Methods are only not included if they cannot be validated.
    '''
    prefix = kwargs.pop('prefix', '')

    frame = get_multilevel_method_sig_frame(header, class_, *args, **kwargs)

    # Extract change event handler method signatures.
    df_sig_handlers = frame[((frame.ndims == 0) | (frame.ndims.isin([None]))) &
                            (frame.method_name.str.match(r'on_' + prefix +
                                                         r'.+_changed'))]

    # Validate signal handlers.
    #
    #  1. All arguments must be of the same type.
    #  2. The number of arguments must be either 0, 1, or 2, corresonding to
    #     the following signatures, respectively:
    #
    #    - `on_<field(s)>_changed()`
    #    - `on_<field(s)>_changed(T new_value)`.
    #    - `on_<field(s)>_changed(T orig_value, T new_value)`.
    frames = []
    for (i, method_name, return_type), df_i in (df_sig_handlers
                                                .groupby(['method_i',
                                                          'method_name',
                                                          'return_atom_type'])):
        arg_count, atom_type = df_i.iloc[0][['arg_count', 'atom_type']]
        if arg_count > 0:
            if not (df_i.atom_type == atom_type).all():
                warnings.warn('[%d] Skipping method due to mixed argument types.')
                continue
        if not (df_i.return_atom_type == 'bool').all():
            warnings.warn('[%d] Skipping method due to incorrect return type '
                          '(should be `bool`).')
            continue
        frames.append(df_i)
    return pd.concat(frames)


def get_handler_template_frame(df_handler_sig, message_type):
    '''
    Return a `pandas.DataFrame` containing only the data required to render the
    validator class code for the specified handler method signatures matching
    the supplied Protocol Buffer message type class.

    The result data frame contains the following columns:

      - `method_name`: Name of method in C class (e.g., `on_value_changed`).
      - `camel_name`: Name of method formatted as camel-case.
      - `arg_count`: The number of arguments to the method (0-2).
      - `atom_type`: The argument type passed to the handler.
      - `tags`: A list of the Protocol Buffer field tag value(s).
      - `depth`: The depth of the field in a nested message (depth is 1 for
        single-level message).
      - `field_name`: The name of the Protocol Buffer field.
    '''
    frames = []

    for (method_name, T, arg_count), df_i in (df_handler_sig
                                              .groupby(['method_name',
                                                        'return_atom_type',
                                                        'arg_count'])):
        try:
            df_parents, s_field = extract_callback_data(get_protobuf_fields_frame
                                                        (message_type), method_name)
            assert((df_i.iloc[0].atom_type is None) or
                   (df_i.iloc[0].atom_type == s_field.atom_type))
        except IndexError:
            warnings.warn('No message field matching method name: %s' % method_name)
            continue
        except AssertionError:
            warnings.warn('[%s] Handler arg type (%s) does not match message field type (%s)'
                          % (method_name, df_i.iloc[0].atom_type, s_field.atom_type))
            continue
        tags = (df_parents.iloc[1:]['parent_field'].map(lambda p: p.number).tolist() +
                [s_field.field_desc.number])
        row = df_i.iloc[0].copy()
        row['tags'] = tags
        row['depth'] = len(tags)
        row['atom_type'] = s_field.atom_type
        row['field_name'] = s_field.name
        frames.append(pd.DataFrame([row]))

    return pd.concat(frames)[['method_name', 'camel_name', 'arg_count',
                              'atom_type', 'tags', 'depth', 'field_name']]


def get_handler_validator_class_code(header, class_, message_type, *args,
                                     **kwargs):
    '''
    Return generated C classes for handlers discovered in provided header/class
    matching the name of the supplied Protocol Buffers message type.

    __NB__ The code returned by this function is not namespaced, and is
    intended to be included as part of a header file.
    '''
    import jinja2

    try:
        df_handler_sig = get_handler_sig_info_frame(header, class_,
                                                    prefix=message_type.DESCRIPTOR
                                                    .name.lower() + '_',
                                                    *args, **kwargs)
    except ValueError:
        df_handlers_template = pd.DataFrame()
    else:
        df_handlers_template = get_handler_template_frame(df_handler_sig,
                                                          message_type)
    template = '''
{%- for i, row in df_handlers.iterrows() -%}
template <typename NodeT>
struct {{ row.camel_name }} : public ScalarFieldValidator<{{ row.atom_type }}, {{ row.depth }}> {
  typedef ScalarFieldValidator<{{ row.atom_type }}, {{ row.depth }}> base_type;

  NodeT *node_p_;
  {{ row.camel_name }}() : node_p_(NULL) { {#- #}
  {%- for t in row.tags %}
    this->tags_[{{ loop.index0 }}] = {{ t }};
  {%- endfor %}
  }

  void set_node(NodeT &node) { node_p_ = &node; }
  virtual bool operator()({{ row.atom_type }} &source, {{ row.atom_type }} target) {
    if (node_p_ != NULL) { return node_p_->{{ row.method_name }}(
    {%- if row.arg_count == 2 %}target, {% endif %}
    {%- if row.arg_count > 0 %}source{% endif %}); }
    return false;
  }
};

{% endfor -%}
template <typename NodeT>
class Validator : public MessageValidator<{{ df_handlers.shape[0] }}> {
public:{# #}
{%- for i, row in df_handlers.iterrows() %}
  {{ row.camel_name }}<NodeT> {{ row.field_name }}_;
{%- endfor %}

  Validator() { {#- #}
{%- for i, row in df_handlers.iterrows() %}
    register_validator({{ row.field_name }}_);
{%- endfor %}
  }

  void set_node(NodeT &node) { {#- #}
{%- for i, row in df_handlers.iterrows() %}
    {{ row.field_name }}_.set_node(node);
{%- endfor %}
  }
};
'''
    return jinja2.Template(template).render(df_handlers=df_handlers_template)


def write_handler_validator_header(output_path, package_name, message_name,
                                   validator_code):
    import jinja2

    template = '''#ifndef ___{{ package_name.upper() }}_{{ message_name.upper() }}_VALIDATE___
#define ___{{ package_name.upper() }}_{{ message_name.upper() }}_VALIDATE___

namespace {{ package_name }} {
namespace {{ message_name }}_validate {

{{ validator_code }}

}  // namespace {{ message_name }}_validate
}  // namespace {{ package_name }}

#endif  // #ifndef ___{{ package_name.upper() }}_{{ message_name.upper() }}_VALIDATE___
    '''
    with open(output_path, 'wb') as output:
        print >> output, (jinja2.Template(template)
                        .render(validator_code=validator_code,
                                package_name=package_name,
                                message_name=message_name))
        print 'Wrote to %s' % output_path
