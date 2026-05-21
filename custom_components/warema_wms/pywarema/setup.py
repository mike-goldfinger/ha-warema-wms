"""Setup script for pywarema library."""

from setuptools import setup, find_packages

setup(
    name="pywarema",
    version="1.0.0",
    description="Python library for Warema WMS radio control system",
    long_description=(
        open("../README.md").read()
        if __import__("os").path.exists("../README.md")
        else ""
    ),
    author="pywarema contributors",
    license="MIT",
    packages=find_packages(),
    install_requires=[
        "pyserial>=3.5",
    ],
    python_requires=">=3.9",
)
