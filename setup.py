# setup.py
"""
Setup configuration for the SmartRetry library.
Enables local installation via: pip install -e .
"""

from setuptools import setup, find_packages

setup(
    name="smartretry",
    version="1.0.0",
    author="Ali Kamrani",
    author_email="kamrani.exe@gmail.com",
    description=(
        "A pure Python, offline-capable decorator library "
        "for handling flaky functions with advanced retry logic."
    ),
    long_description=open("README.md").read() if __import__("os").path.exists("README.md") else "",
    long_description_content_type="text/markdown",
    url="https://github.com/MRThugh/SmartRetry",
    packages=find_packages(),
    python_requires=">=3.8",
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Intended Audience :: Developers",
        "Topic :: Software Development :: Libraries :: Python Modules",
    ],
    keywords="retry decorator backoff resilience flaky",
)
