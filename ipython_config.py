c = get_config()

# Exclude % and %% magic names from completion suggestions.
c.IPCompleter.disable_matchers = ["IPCompleter.magic_matcher"]
