import os
from setuptools import setup, find_packages

def read(fname):
    return open(os.path.join(os.path.dirname(__file__), fname)).read()

setup(
    name = "stompest",
    version = "1.0.1",
    author = "Roger Hoover",
    author_email = "roger.hoover@gmail.com",
    description = "STOMP client library for Python including both synchronous and Twisted implementations.",
    license = 'Apache License 2.0',
    packages = find_packages(),
    long_description=read('README.markdown'),
    keywords = "stomp twisted activemq",
    url = "https://github.com/mozes/stompest",
    py_modules=["stompest"],
    include_package_data = True,
    zip_safe = False,   
    install_requires = [
        'stomper',
        'twisted',
        'mock',
    ],
    test_suite = 'stompest.tests',
    classifiers=[
        "Development Status :: 5 - Production/Stable",
        "Framework :: Twisted",
        "Topic :: System :: Networking",
        "Operating System :: OS Independent",
        "License :: OSI Approved :: Apache Software License",
    ],
)
