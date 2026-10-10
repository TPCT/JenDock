"""Exact ordered Compose invocation identity, without filesystem normalization."""
def ordered_files(values):
    if (not isinstance(values, list) or not values
            or not all(isinstance(path, str) and path.startswith('/')
                       and not any(ord(char) < 32 or ord(char) == 127 for char in path) for path in values)
            or len(set(values)) != len(values)):
        raise ValueError('Unique absolute Compose files in their recorded order are required.')
    return tuple(values)
