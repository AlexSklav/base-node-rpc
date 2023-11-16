# coding: utf-8
from platformio_helpers.upload import upload_conda, parse_args


if __name__ == '__main__':
    args = parse_args(project_name='base-node-rpc')
    extra_args = ['--upload-port', args.port] if args.port else []
    print(upload_conda('base-node-rpc', env_name=args.env_name,
                       extra_args=extra_args))
