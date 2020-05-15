import os
import re
import json
import subprocess
import shutil
import unicodedata

from ruamel.yaml import YAML
from jina.flow import Flow
from builder.color_print import *
yaml = YAML()


class Target:

    def __init__(self, path):
        self.path = path
        self.canonic_name = str(os.path.relpath(path).replace('/', '.').strip('.')[4:])
        self.manifest_path = os.path.join(path, 'manifest.yml')
        self.dockerfile_path = os.path.join(path, 'Dockerfile')
        self.readme_path = os.path.join(path, 'README.md')
        self.manifest = self.safe_load_manifest()

    def load_manifest(self):
        with open(self.manifest_path) as yml:
            manifest = yaml.load(yml)
        return manifest

    def safe_load_manifest(self):
        print(print_green('\nManifest file ') + self.manifest_path)
        manifest = self.load_manifest()
        self.check_manifest(manifest)
        self.add_platform_revision_source(manifest)
        for key, value in manifest.items():
            updated_value = self.remove_control_characters(value)
            if updated_value != value:
                manifest[key] = updated_value
                print(print_purple(f'{key}: "{value}" -> "{updated_value}"') + ' // removed invalid chars')
            else:
                print(print_purple(f'{key}: {value}'))

        return manifest

    @staticmethod
    def check_manifest(manifest):
        allowed = {'name', 'description', 'author', 'url', 'documentation', 'version',
                   'vendor', 'license', 'avatar', 'platform', 'update'}
        required = {'name', 'description'}
        keys = set(manifest.keys())
        if len(required - keys) > 0:
            raise ValueError(f"{required - keys} required!")
        elif len(keys - allowed) > 0:
            raise ValueError(f"{keys - allowed} are not allowed!")

    def check_image_canonic_name(self):
        image_tag_regex = r'^hub.[a-zA-Z_$][a-zA-Z_\s\-\.$0-9]*$'
        if not re.match(image_tag_regex, self.canonic_name):
            raise ValueError(
                f'{self.canonic_name} is not a valid image name '
                f'for a Jina Hub image, it should match with {image_tag_regex}'
            )

    @staticmethod
    def remove_control_characters(source):
        return ''.join(ch for ch in source if not unicodedata.category(ch).startswith('C'))

    @staticmethod
    def add_platform_revision_source(manifest):
        manifest['platform'] = manifest.get('platform', [])
        manifest['revision'] = subprocess.check_output(['git', 'rev-parse', '--short', 'HEAD']).strip().decode()
        manifest['source'] = 'https://github.com/jina-ai/jina-hub/commit/' + manifest['revision']

    def update_dockerfile_with_label(self):
        label_prefix = 'ai.jina.hub.'
        revised_dockerfile = []

        with open(self.dockerfile_path) as dockerfile:
            for line in dockerfile:
                revised_dockerfile.append(line)
                if line.startswith('FROM'):
                    revised_dockerfile.append('LABEL ')
                    revised_dockerfile.append(
                        ' \\      \n'.join(f'{label_prefix}{k}="{v}"' for k, v in self.manifest.items())
                    )
        print(print_green('\nDockerfile ') + self.dockerfile_path)
        for line in revised_dockerfile:
            if line != '\n':
                print(print_purple(line.strip('\n')))
        with open(self.dockerfile_path + '.tmp', 'w') as fp:
            fp.writelines(revised_dockerfile)

    def build_image(self, test=False, push=False):
        self.update_dockerfile_with_label()
        self.check_image_canonic_name()
        self.add_jina_source()

        docker_registry = 'jinaai/'
        full_image_name = f'{docker_registry}{self.canonic_name}:{self.manifest["version"]}'
        docker_cmd = self.prepare_docker_cmd(
            docker_registry=docker_registry, full_image_name=full_image_name, push=push
        )

        print(print_green('\nStarting docker build for image ') + self.canonic_name)
        subprocess.check_call(docker_cmd)

        if test:
            self.test_image(full_image_name)
        if push:
            self.push_image_readme()

        self.pull_image(full_image_name)
        tmp = subprocess.check_output(['docker', 'inspect', full_image_name]).strip().decode()
        docker_inspect_output = json.loads(tmp)[0]
        self.update_target_readme()
        print(print_green(f'Successfully built {"and pushed " if push else ""}image ') + self.canonic_name + '\n')
        return docker_inspect_output

    def prepare_docker_cmd(self, docker_registry, full_image_name, push):
        dockerbuild_cmd = ['docker', 'buildx', 'build']
        tag = f'{docker_registry}{self.canonic_name}:latest'
        dockerbuild_args = ['-t', full_image_name, '-t', tag, '--file', self.dockerfile_path + '.tmp']
        dockerbuild_platform = ['--platform', ','.join(v for v in self.manifest['platform'])] if self.manifest[
            'platform'] else []
        dockerbuild_action = '--push' if push else '--load'
        return dockerbuild_cmd + dockerbuild_platform + dockerbuild_args + [dockerbuild_action, self.path]

    def add_jina_source(self):
        if not os.path.isdir(os.path.join(self.path, 'jina')):
            root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            jinasrc_dir = os.path.join(root_dir, 'src', 'jina')
            shutil.copytree(src=jinasrc_dir, dst=os.path.join(self.path, 'jina'))

    @staticmethod
    def test_image(full_image_name):
        print(print_green('Testing docker run for image ') + full_image_name)
        subprocess.check_call(['docker', 'run', '--rm', full_image_name, '--max-idle-time', '5', '--shutdown-idle'])

        print(print_green('Testing jina cli for image ') + full_image_name)
        subprocess.check_call(['jina', 'pod', '--image', full_image_name, '--max-idle-time', '5', '--shutdown-idle'])

        print(print_green('Testing jina flow API for image ') + full_image_name)
        with Flow().add(image=full_image_name, replicas=3).build():
            pass
        print(print_green('All tests passed successfully for image ') + full_image_name)

    def push_image_readme(self):
        docker_registry = 'jinaai/'
        docker_cmd = [
            'docker', 'run', '-v', f'{self.path}:/workspace',
            '-e', f'DOCKERHUB_USERNAME={os.environ["DOCKERHUB_DEVBOT_USER"]}',
            '-e', f'DOCKERHUB_PASSWORD={os.environ["DOCKERHUB_DEVBOT_TOKEN"]}',
            '-e', f'DOCKERHUB_REPOSITORY={docker_registry}{self.canonic_name}',
            '-e', 'README_FILEPATH=/workspace/README.md',
            'peterevans/dockerhub-description:2.1'
        ]
        subprocess.check_call(docker_cmd)
        print(print_green('Successfully pushed readme for image ') + self.canonic_name)

    def update_target_readme(self):
        with open(self.readme_path, 'w') as fp:
            fp.write('#{name}\n\n#{description}\n'.format_map(self.manifest))

    @staticmethod
    def pull_image(full_image_name):
        print(print_green('Pulling image ') + full_image_name)
        subprocess.check_call(['docker', 'pull', full_image_name])
