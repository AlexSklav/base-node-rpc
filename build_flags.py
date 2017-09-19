import argparse
import sys

import base_node_rpc.version


def parse_args(args=None):
    if args is None:
        args = sys.argv[1:]
    parser = argparse.ArgumentParser()

    default_version = base_node_rpc.version.getVersion()
    parser.add_argument('-V', '--version', default=default_version)
    parser.add_argument('arg', nargs='*')

    return parser.parse_known_args(args=args)


if __name__ == '__main__':
    args, extra_args = parse_args()

    extra_args += [r'-DDEVICE_ID_RESPONSE=\"base-node-rpc::{}\"'
                   .format(args.version)]

    print ' '.join(extra_args)
