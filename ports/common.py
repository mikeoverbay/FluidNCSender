"""What every ports module shares."""

SPEEDS = (9600, 19200, 38400, 57600, 115200, 230400, 460800, 500000, 921600, 1000000, 1500000, 2000000)


class PortError(Exception):
    """A port that couldn't be opened. The message is for the user; status is the HTTP status to answer with."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status
