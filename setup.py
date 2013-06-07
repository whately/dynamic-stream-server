import sys
try:
    from setuptools import setup
except ImportError:
    from distribute_setup import use_setuptools
    use_setuptools()
    from setuptools import setup

py_version = sys.version_info[:2]

install_requires = []
if py_version < (3, 2):
    install_requires += [
        'futures',
        'configparser',
    ]

# All versions
install_requires += [
    'setuptools',
    'makeobj',
]

setup(
    name='Dynamic Stream Server',
    version = '0.1',
    description = 'Dynamic video streaming system',
    author = [
        'Joao Bernardo Oliveira',
        'Nelson Perez',
    ],
    author_email = 'jbvsmo@gmail.com',
    url = 'https://bitbucket.org/jbvsmo/dynamic-stream-server',
    packages = [],
    install_requires = install_requires,
)