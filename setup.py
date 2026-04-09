from setuptools import setup, find_packages

setup(
    name="repoanalyzer-v2",
    version="2.0.0",
    description="Agentic AI-powered code repository analyzer with multi-LLM support",
    python_requires=">=3.9",
    packages=find_packages(),
    install_requires=["reportlab>=4.0.0"],
    extras_require={"bedrock": ["boto3>=1.26.0"], "all": ["boto3>=1.26.0"]},
    entry_points={"console_scripts": ["repoanalyzer=repoanalyzer_v2.__main__:main"]},
)
