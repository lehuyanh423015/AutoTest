from setuptools import setup

setup(
    python_requires=">=3.10",
    install_requires=["requests>=2", "click>=8"],
    extras_require={"test": ["pytest>=8"]},
)
