from setuptools import setup

raise RuntimeError("SETUP.PY MUST NOT RUN")


def load_requirements():
    raise RuntimeError("DEPENDENCIES MUST NOT RUN")


setup(install_requires=load_requirements())
