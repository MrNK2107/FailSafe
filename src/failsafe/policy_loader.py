import json
from pathlib import Path


def load_policy(path, ReasonEnum=None, ActionEnum=None, extra_validators=None):
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    validated = {}
    for key, entry in raw.items():
        if key.startswith("_"):
            continue
        if ReasonEnum is not None:
            try:
                ReasonEnum(key)
            except ValueError:
                raise ValueError(
                    f"{Path(path).name}: {key!r} is not a known {ReasonEnum.__name__}; "
                    f"must be one of {[r.value for r in ReasonEnum]}"
                )
        if ActionEnum is not None:
            try:
                ActionEnum(entry["allowed_action"])
            except ValueError:
                raise ValueError(
                    f"{Path(path).name}: {key!r} has invalid allowed_action {entry['allowed_action']!r}; "
                    f"must be one of {[a.value for a in ActionEnum]}"
                )
        for rate_field in ("simulated_success_rate", "simulated_recovery_rate"):
            if rate_field in entry:
                rate = entry[rate_field]
                if (
                    not isinstance(rate, (int, float))
                    or isinstance(rate, bool)
                    or not (0.0 <= rate <= 1.0)
                ):
                    raise ValueError(
                        f"{Path(path).name}: {key!r} has invalid {rate_field} {rate!r}; "
                        f"must be a number between 0.0 and 1.0"
                    )
        if extra_validators:
            for field, enum_cls in extra_validators.items():
                if field in entry:
                    try:
                        enum_cls(entry[field])
                    except ValueError:
                        raise ValueError(
                            f"{Path(path).name}: {key!r} has invalid {field} {entry[field]!r}; "
                            f"must be one of {[s.value for s in enum_cls]}"
                        )
        validated[key] = entry
    return validated
