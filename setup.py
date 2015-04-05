import sys
from setuptools import setup, find_packages
from setuptools.command.test import test as TestCommand


class PyTest(TestCommand):
    user_options = [('pytest-args=', 'a', "Arguments to pass to py.test")]

    def initialize_options(self):
        TestCommand.initialize_options(self)
        self.pytest_args = []

    def finalize_options(self):
        TestCommand.finalize_options(self)
        self.test_args = []
        self.test_suite = True

    def run_tests(self):
        # import here, cause outside the eggs aren't loaded
        import pytest
        errno = pytest.main(self.pytest_args)
        sys.exit(errno)


console_scripts = []

cmdclass = dict(test=PyTest)

install_requires = set(x.strip() for x in open('requirements.txt'))

install_requires_replacements = {
    'https://github.com/ethereum/pyrlp/tarball/develop': 'rlp>=0.3.7'}
install_requires = [install_requires_replacements.get(r, r) for r in install_requires]

setup(name="ethereum",
      packages=find_packages("."),
      description='Next generation cryptocurrency network',
      url='https://github.com/ethereum/pyethereum/',
      install_requires=install_requires,
      entry_points=dict(console_scripts=console_scripts),
      version='0.9.61',
      cmdclass=cmdclass
      )
