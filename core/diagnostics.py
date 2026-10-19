"""Safe exception location without messages, local variables or request data."""

import logging
import traceback


def report_input_error(exc: Exception) -> str:
    frames = traceback.extract_tb(exc.__traceback__)
    # Only static code locations; never format the exception or source lines.
    locations = "/".join(f"{frame.name}:{frame.lineno}" for frame in frames[-8:])
    kind = type(exc).__name__
    code = f"{kind}:{frames[-1].name}:{frames[-1].lineno}" if frames else kind
    logging.getLogger(__name__).error("LEGAL_AI_INPUT_ERROR %s locations=%s", code, locations)
    return code
