from app.repositories.ledger.repositories import CellEventRepository, FillRepository


def test_ledger_repositories_do_not_expose_mutation_methods() -> None:
    for repository in (CellEventRepository, FillRepository):
        assert not hasattr(repository, "update")
        assert not hasattr(repository, "delete")
