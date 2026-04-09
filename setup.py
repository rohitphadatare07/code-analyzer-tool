from setuptools import setup, find_packages

setup(
    name="repoanalyzer",
    version="1.0.0",
    description="AI-powered code repository analyzer with multi-LLM support and PDF report generation",
    long_description=open("README.md").read() if __import__("os").path.exists("README.md") else "",
    author="RepoAnalyzer",
    python_requires=">=3.9",
    packages=find_packages(),
    install_requires=[
        "reportlab>=4.0.0",
    ],
    extras_require={
        "bedrock": ["boto3>=1.26.0"],
        "all": ["boto3>=1.26.0"],
    },
    entry_points={
        "console_scripts": [
            "repoanalyzer=repoanalyzer.__main__:main",
        ],
    },
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
)
