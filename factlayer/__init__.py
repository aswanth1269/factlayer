"""Fact knowledge layer.

Loading the .env file here means every entry point picks up the API key without
each one having to remember to do it.
"""

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # dotenv is optional; environment variables still work
    pass
