class ConfigError(Exception):
    """Base class for all configuration-related errors."""

    pass


class ConfigFileNotFoundError(ConfigError):
    def __init__(self, config_file, message="Configuration file not found"):
        self.config_file = config_file
        self.message = f"{message}: {config_file}"
        super().__init__(self.message)


class ConfigValidationError(ConfigError):
    def __init__(self, missing_fields=None, invalid_fields=None, message="Configuration validation error"):
        self.missing_fields = missing_fields or []
        self.invalid_fields = invalid_fields or []
        details = []
        if self.missing_fields:
            details.append(f"Missing required fields - {', '.join(self.missing_fields)}")
        if self.invalid_fields:
            details.append(f"Invalid fields - {', '.join(self.invalid_fields)}")
        self.message = f"{message}: {'; '.join(details)}. Check your config.json against the example configuration."
        super().__init__(self.message)


class ConfigParseError(ConfigError):
    def __init__(self, config_file, original_exception, message="Error parsing configuration file"):
        self.config_file = config_file
        self.original_exception = original_exception
        self.message = f"{message} ({config_file}): {original_exception!s}"
        super().__init__(self.message)
