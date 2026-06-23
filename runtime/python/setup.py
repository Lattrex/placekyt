"""
Setup script for gr_kyttar - GNURadio blocks for Kyttar simulation.

Install with:
    pip install -e .

Requirements:
    - gnuradio (optional for development, required for runtime)
    - numpy
    - kyttar (the Rust-based simulator, built with maturin)
"""

from setuptools import setup, find_packages

setup(
    name="gr_kyttar",
    version="0.1.0",
    description="GNURadio blocks for Kyttar simulation",
    long_description=open("README.md").read() if __file__ else "",
    long_description_content_type="text/markdown",
    author="Lattrex",
    author_email="",
    url="https://github.com/kyttar-project/kyttar",
    packages=find_packages(),
    python_requires=">=3.8",
    install_requires=[
        "numpy>=1.20.0",
        "PyYAML>=5.0",
    ],
    extras_require={
        "gnuradio": [],  # GNURadio is system-installed, not pip
        "dev": [
            "pytest",
            "maturin",
        ],
    },
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "Intended Audience :: Science/Research",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Topic :: Scientific/Engineering :: Electronic Design Automation (EDA)",
        "Topic :: Software Development :: Embedded Systems",
    ],
    keywords="gnuradio, dsp, simulation, kyttar, asynchronous",
)
