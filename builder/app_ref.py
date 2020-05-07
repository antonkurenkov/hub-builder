import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import unicodedata
import string
from datetime import datetime
from pathlib import Path

from jina.flow import Flow
from ruamel.yaml import YAML

yaml = YAML()
allowed = {'name', 'description', 'author', 'url', 'documentation', 'version', 'vendor', 'license', 'avatar',
           'platform'}
required = {'name', 'description'}
sver_regex = r'^(=|>=|<=|=>|=<|>|<|!=|~|~>|\^)?(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)' \
             r'\.(?P<patch>0|[1-9]\d*)(?:-(?P<prerelease>(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)' \
             r'(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?(?:\+(?P<buildmetadata>[0-9a-zA-Z-]+' \
             r'(?:\.[0-9a-zA-Z-]+)*))?$'
name_regex = r'^[a-zA-Z_$][a-zA-Z_\s\-$0-9]{2,20}$'

cur_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

jinasrc_dir = os.path.join(root_dir, 'src', 'jina')
image_tag_regex = r'^hub.[a-zA-Z_$][a-zA-Z_\s\-\.$0-9]*$'
label_prefix = 'ai.jina.hub.'
docker_registry = 'jinaai/'

# current date and time
builder_files = list(Path(root_dir).glob('builder/app.py')) + \
                list(Path(root_dir).glob('builder/*.yml'))

build_hist_path = os.path.join(root_dir, 'status', 'build-history.json')
readme_path = os.path.join(root_dir, 'status', 'README.md')
hubbadge_path = os.path.join(root_dir, 'status', 'hub-stat.svg')

hub_files = list(Path(root_dir).glob('hub/**/*.y*ml')) + \
            list(Path(root_dir).glob('hub/**/*Dockerfile')) + \
            list(Path(root_dir).glob('hub/**/*.py'))

builder_revision = subprocess.check_output(['git', 'rev-parse', '--short', 'HEAD']).strip().decode()
build_badge_regex = r'<!-- START_BUILD_BADGE -->(.*)<!-- END_BUILD_BADGE -->'
build_badge_prefix = r'<!-- START_BUILD_BADGE --><!-- END_BUILD_BADGE -->'


class Validator:

    def __init__(self, manifest=None, image_canonical_name=''):
        self.manifest = manifest
        self.image_canonical_name = image_canonical_name

    def validate_schema(self):
        keys = set(self.manifest.keys())
        if len(required - keys) > 0:
            raise ValueError(f"{required - keys} required!")
        elif len(keys - allowed) > 0:
            raise ValueError(f"{keys - allowed} are not allowed!")

    def check_chain(self):
        self.check_image_name()
        self.check_name()
        self.check_version()
        self.check_license()
        self.check_platform()

    def check_name(self):
        name = self.manifest.get('name')
        if not re.match(name_regex, name):
            raise ValueError(f'{name} is not a valid name, it should match with {name_regex}')

    def check_version(self):
        version = self.manifest.get('version')
        if not re.match(sver_regex, version):
            raise ValueError(f'{version} is not a valid semantic version number, see http://semver.org/')

    def check_image_name(self):
        if not re.match(image_tag_regex, self.image_canonical_name):
            raise ValueError(
                f'{self} is not a valid image name for a Jina Hub image, it should match with {image_tag_regex}'
            )

    def check_platform(self):
        with open(os.path.join(cur_dir, 'platforms.yml')) as yml:
            supported_platforms = yaml.load(yml)

        for user_added_platform in self.manifest.get('platform'):
            if user_added_platform not in supported_platforms:
                raise ValueError(
                    f'platform {user_added_platform} is not supported, should be one of {supported_platforms}'
                )

    def check_license(self):
        license_ = self.manifest.get('license')
        with open(os.path.join(cur_dir, 'osi-approved.yml')) as fp:
            approved = yaml.load(fp)
        if license_ not in approved:
            raise ValueError(f"license {license_} is not an OSI-approved license {approved}")
        return approved[license_]


class SingleBuilder(Validator):

    def __init__(self, args):
        super().__init__()
        self.target: str = args.target
        self.reason: str = args.reason
        self.push: bool = args.push
        self.test: bool = args.test
        self.error_on_empty: bool = args.error_on_empty
        self.check_only: bool = args.check_only
        self.bleach_first: bool = args.bleach_first

        self.dockerfile_path = None
        self.manifest_path = None
        self.image_canonical_name = None
        self.manifest = None

    def run(self):
        if self.construct_paths():
            self.load_manifest()
            self.validate_schema()
            self.update_fields()

            self.check_chain()

            if self.bleach_first:
                self.clean_docker()

            self.build_image()
            self.pushing()
            img_name = f'{docker_registry}{self.image_canonical_name}:{self.manifest["version"]}'
            self.testing(img_name)

    def construct_paths(self):
        if os.path.isdir(self.target):
            self.dockerfile_path = os.path.join(self.target, 'Dockerfile')
            self.manifest_path = os.path.join(self.target, 'manifest.yml')
            self.image_canonical_name = os.path.relpath(self.target).replace('/', '.')[3:]
            return True
        elif self.error_on_empty:
            raise NotADirectoryError(f'{self.target} is not a valid directory')

    def load_manifest(self):
        with open(self.manifest_path) as yml:
            self.manifest = yaml.load(yml)

    def update_fields(self):
        for key, value in self.manifest.items():
            updated_value = self.remove_control_characters(value)
            if updated_value != value:
                self.manifest[key] = updated_value
                print(f'{key}: {value} -> {updated_value} // removed invalid chars')
            else:
                print(f'{key}: {value}')
        self.add_platform_revision_source()

    @staticmethod
    def remove_control_characters(source):
        return ''.join(ch for ch in source if not unicodedata.category(ch).startswith('C'))

    def add_platform_revision_source(self):
        self.manifest['platform'] = self.manifest.get('platform') or []
        print(f"platform: {self.manifest['platform']}")

        self.manifest['revision'] = subprocess.check_output(['git', 'rev-parse', '--short', 'HEAD']).strip().decode()
        print(f"revision: {self.manifest['revision']}")

        self.manifest['source'] = 'https://github.com/jina-ai/jina-hub/commit/' + self.manifest['revision']
        print(f"source: {self.manifest['source']}")

    @staticmethod
    def clean_docker():
        for k in ['df -h',
                  'docker stop $(docker ps -aq)',
                  'docker rm $(docker ps -aq)',
                  'docker rmi $(docker image ls -aq)',
                  'df -h']:
            try:
                subprocess.check_call(k, shell=True)
            except subprocess.CalledProcessError:
                pass

    def build_image(self):
        revised_dockerfile = []
        with open(self.dockerfile_path) as dockerfile:
            for line in dockerfile:
                revised_dockerfile.append(line)
                if line.startswith('FROM'):
                    revised_dockerfile.append('LABEL ')
                    revised_dockerfile.append(
                        ' \\      \n'.join(f'{label_prefix}{k}="{v}"' for k, v in self.manifest.items())
                    )
        for k in revised_dockerfile:
            print(k)

        with open(self.dockerfile_path + '.tmp', 'w') as fp:
            fp.writelines(revised_dockerfile)

        shutil.copytree(src=jinasrc_dir, dst=os.path.join(self.target, 'jina'))

        dockerbuild_cmd = ['docker', 'buildx', 'build']
        dockerbuild_args = [
            '-t', f'{docker_registry}{self.image_canonical_name}:{self.manifest["version"]}', '-t',
            f'{docker_registry}{self.image_canonical_name}:latest',
            '--file', self.dockerfile_path + '.tmp'
        ]
        dockerbuild_platform = ['--platform', ','.join(v for v in self.manifest['platform'])] \
            if self.manifest['platform'] else []
        dockerbuild_action = '--push' if self.push else '--load'
        docker_cmd = dockerbuild_cmd + dockerbuild_platform + dockerbuild_args + [dockerbuild_action, self.target]
        subprocess.check_call(docker_cmd)
        print('building finished successfully!')

    def testing(self, img_name):
        if self.test:
            print('testing image with docker run')
            subprocess.check_call(['docker', 'run', '--rm', img_name, '--max-idle-time', '5', '--shutdown-idle'])

            print('testing image with jina cli')
            subprocess.check_call(['jina', 'pod', '--image', img_name, '--max-idle-time', '5', '--shutdown-idle'])

            print('testing image with jina flow API')
            with Flow().add(image=img_name, replicas=3).build():
                pass
            print('all tests passed successfully!')

    def pushing(self):
        if self.push:
            target_readme_path = os.path.join(self.target, 'README.md')
            if not os.path.exists(target_readme_path):
                with open(target_readme_path, 'w') as fp:
                    fp.write('#{name}\n\n#{description}\n'.format_map(self.manifest))

            docker_readme_cmd = ['docker', 'run', '-v', f'{self.target}:/workspace',
                                 '-e', f'DOCKERHUB_USERNAME={os.environ["DOCKERHUB_DEVBOT_USER"]}',
                                 '-e', f'DOCKERHUB_PASSWORD={os.environ["DOCKERHUB_DEVBOT_PWD"]}',
                                 '-e', f'DOCKERHUB_REPOSITORY={docker_registry}{self.image_canonical_name}',
                                 '-e', 'README_FILEPATH=/workspace/README.md',
                                 'peterevans/dockerhub-description:2.1']
            subprocess.check_call(docker_readme_cmd)
            print('upload readme success!')


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
    parser.add_argument('--check-only', action='store_true', default=False,
                        help='check if the there is anything to update')
    parser.add_argument('--bleach-first', action='store_true', default=False,
                        help='clear docker before starting the build')
    return parser


if __name__ == '__main__':
    args = get_parser().parse_args()
    if args.check_only:
        pass
        # t = get_update_targets()[0]
        # if t:
        #     exit(0)
        # else:
        #     # nothing to update exit with 1
        #     exit(1)
    if args.target:
        builder = SingleBuilder(args)
        builder.run()
    else:
        pass
        # builder = MultiBuilder(args)