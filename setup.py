#!/usr/bin/env python

import os
import re
from setuptools import setup
try:
    # pip >=20
    from pip._internal.network.session import PipSession
    from pip._internal.req import parse_requirements
except ImportError:
    try:
        # 10.0.0 <= pip <= 19.3.1
        from pip._internal.download import PipSession
        from pip._internal.req import parse_requirements
    except ImportError:
        # pip <= 9.0.3
        from pip.download import PipSession
        from pip.req import parse_requirements
from setuptools.command.install import install
from setuptools.command.develop import develop


# Hackishly override of the install method
class InstallReqs(install):
    def run(self):
        print(" ****************************** ")
        print(" *** Installing VCAL-SPHERE *** ")
        print(" ****************************** ")
        os.system('pip install -r requirements.txt')
        install.run(self)


class InstallDevReqs(develop):
    def run(self):
        print(" ****************************** ")
        print(" *** Installing VCAL-SPHERE *** ")
        print(" ****************************** ")
        os.system('pip install -r requirements-dev.txt')
        develop.run(self)


def resource(*args):
    return os.path.join(os.path.abspath(os.path.join(__file__, os.pardir)),
                        *args)


# parse_requirements() returns generator of pip.req.InstallRequirement objects
reqs = parse_requirements(resource('requirements.txt'), session=False)
try:
    reqs = [str(ir.req) for ir in reqs]
except:
    reqs = [str(ir.requirement) for ir in reqs]
reqs_dev = parse_requirements(resource('requirements-dev.txt'), session=False)
try:
    reqs_dev = [str(ir.req) for ir in reqs_dev]
except:
    reqs_dev = [str(ir.requirement) for ir in reqs_dev]    

with open(resource('README.rst')) as readme_file:
    README = readme_file.read()

with open(resource('vcal', '__init__.py')) as version_file:
    version_file = version_file.read()
    VERSION = re.search(r"""^__version__ = ['"]([^'"]*)['"]""",
                        version_file, re.M)
    VERSION = VERSION.group(1)

PACKAGES = ['vcal',
            'vcal.calib',
            'vcal.postproc',
            'vcal.preproc',
            'vcal.utils']

setup(
    name='vcal',
    version=VERSION,
    description='Package for VIP-based SPHERE image Calibration and processing',
    long_description=README,
    license='MIT',
    author='Valentin Christiaens',
    author_email='valentinchrist@hotmail.com',
    url='https://github.com/VChristiaens/vcal_sphere',
    cmdclass={'install': InstallReqs,
              'develop': InstallDevReqs},
    packages=PACKAGES,
    install_requires=reqs,
    extras_require={"dev": reqs_dev},
    zip_safe=False,
    classifiers=['Intended Audience :: Science/Research',
                 'License :: OSI Approved :: MIT License',
                 'Operating System :: MacOS :: MacOS X',
                 'Operating System :: POSIX :: Linux',
                 'Natural Language :: English',
                 'Programming Language :: Python :: 3.6',
                 'Programming Language :: Python :: 3.7',
                 'Topic :: Scientific/Engineering :: Astronomy'
                 ]
)
