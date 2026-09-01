from decimal import Decimal, localcontext


class VectorNormalizer:
    """Deterministic population z-score normalization."""

    version = "ZSCORE-NORM-v1"

    def fit_transform(
        self, vectors: list[dict[str, Decimal]]
    ) -> tuple[list[dict[str, Decimal]], dict[str, dict[str, Decimal | bool | int | str]]]:
        if not vectors:
            return [], {}
        names = tuple(sorted(vectors[0]))
        if any(tuple(sorted(vector)) != names for vector in vectors):
            raise ValueError("analog vectors must share an identical feature schema")
        params: dict[str, dict[str, Decimal | bool | int | str]] = {}
        with localcontext() as context:
            context.prec = 34
            count = Decimal(len(vectors))
            for name in names:
                values = [Decimal(vector[name]) for vector in vectors]
                mean = sum(values, Decimal("0")) / count
                variance = sum(((value - mean) ** 2 for value in values), Decimal("0")) / count
                std = variance.sqrt()
                params[name] = {
                    "mean": mean,
                    "std_dev": std,
                    "sample_count": len(vectors),
                    "zero_variance": std == 0,
                    "policy_version": self.version,
                }
            normalized = [
                {
                    name: Decimal("0")
                    if params[name]["zero_variance"]
                    else (Decimal(vector[name]) - Decimal(params[name]["mean"])) / Decimal(params[name]["std_dev"])
                    for name in names
                }
                for vector in vectors
            ]
        return normalized, params

    @staticmethod
    def distance(left: dict[str, Decimal], right: dict[str, Decimal]) -> Decimal:
        if set(left) != set(right):
            raise ValueError("normalized vectors must share an identical feature schema")
        with localcontext() as context:
            context.prec = 34
            return sum(((Decimal(left[name]) - Decimal(right[name])) ** 2 for name in sorted(left)), Decimal("0")).sqrt()
