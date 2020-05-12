import argparse
import subprocess
from builder.modules.build import SingleBuilder, MultiBuilder


def get_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument('--target', type=str,
                        help='the directory path of target Pod image, where manifest.yml and Dockerfile located')
    gp1 = parser.add_mutually_exclusive_group()
    gp1.add_argument('--push', action='store_true', default=False,
                     help='push to the registry')
    gp1.add_argument('--test', action='store_true', default=False,
                     help='test the pod image')
    parser.add_argument('--error-on-empty', action='store_true', default=False,
                        help='stop and raise error when the target is empty, otherwise just gracefully exit')
    parser.add_argument('--reason', type=str, nargs='*',
                        help='the reason of the build')
    parser.add_argument('--check-targets', action='store_true', default=False,
                        help='check if there is anything to update')
    parser.add_argument('--bleach-first', action='store_true', default=False,
                        help='clear docker before starting the build')
    return parser


def clean_docker():
    print('\033[32m' + 'Removing all existing docker instances' + '\033[0m')
    for k in ['df -h',
              'docker stop $(docker ps -aq)',
              'docker rm $(docker ps -aq)',
              'docker rmi -f $(docker image ls -aq)',
              'df -h']:
        try:
            subprocess.check_call(k, shell=True)
        except subprocess.CalledProcessError:
            pass


if __name__ == '__main__':
    args = get_parser().parse_args()
    if args.bleach_first:
        clean_docker()
    if args.target and not args.check_targets:
        builder = SingleBuilder(args)
    else:
        builder = MultiBuilder(args)
    builder.run()

#
