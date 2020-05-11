from ruamel.yaml import YAML
import os
import re

cur_dir = os.path.dirname(os.path.abspath(__file__))

osi_approved_yml_path = os.path.join(os.path.dirname(cur_dir), 'osi-approved.yml')
platforms_yml_path = os.path.join(os.path.dirname(cur_dir), 'platforms.yml')

allowed = {'name', 'description', 'author', 'url', 'documentation', 'version', 'vendor', 'license', 'avatar',
           'platform'}
required = {'name', 'description'}

image_tag_regex = r'^hub.[a-zA-Z_$][a-zA-Z_\s\-\.$0-9]*$'
sver_regex = r'^(=|>=|<=|=>|=<|>|<|!=|~|~>|\^)?(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)' \
             r'\.(?P<patch>0|[1-9]\d*)(?:-(?P<prerelease>(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)' \
             r'(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?(?:\+(?P<buildmetadata>[0-9a-zA-Z-]+' \
             r'(?:\.[0-9a-zA-Z-]+)*))?$'
name_regex = r'^[a-zA-Z_$][a-zA-Z_\s\-$0-9]{2,20}$'

yaml = YAML()


class Validator:

    def __init__(self):
        self.manifest = None
        self.canonic_name = None

    def validate_schema(self):
        keys = set(self.manifest.keys())
        if len(required - keys) > 0:
            raise ValueError(f"{required - keys} required!")
        elif len(keys - allowed) > 0:
            raise ValueError(f"{keys - allowed} are not allowed!")

    def check_chain(self):
        self.check_image_canonical_name()
        self.check_name()
        self.check_version()
        self.check_license()
        self.check_platform()

    def check_image_canonical_name(self):
        if not re.match(image_tag_regex, self.canonic_name):
            raise ValueError(
                f'{self.canonic_name} is not a valid image name '
                f'for a Jina Hub image, it should match with {image_tag_regex}'
            )

    def check_name(self):
        name = self.manifest.get('name')
        if not re.match(name_regex, name):
            raise ValueError(f'{name} is not a valid name, it should match with {name_regex}')

    def check_version(self):
        version = self.manifest.get('version')
        if not re.match(sver_regex, version):
            raise ValueError(f'{version} is not a valid semantic version number, see http://semver.org/')

    def check_license(self):
        license_ = self.manifest.get('license')
        with open(osi_approved_yml_path) as fp:
            approved = yaml.load(fp)
        if license_ not in approved:
            raise ValueError(f"license {license_} is not an OSI-approved license {approved}")
        return approved[license_]

    def check_platform(self):
        with open(platforms_yml_path) as yml:
            supported_platforms = yaml.load(yml)

        for user_added_platform in self.manifest.get('platform'):
            if user_added_platform not in supported_platforms:
                raise ValueError(
                    f'platform {user_added_platform} is not supported, should be one of {supported_platforms}'
                )

