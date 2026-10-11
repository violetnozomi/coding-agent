from openlibrary.book_providers import (
    PROVIDER_ORDER,
    ProjectRunebergProvider,
    get_book_provider_by_name,
)


class TestProjectRunebergProvider:
    def test_short_name(self):
        assert ProjectRunebergProvider().short_name == 'runeberg'

    def test_registered_in_provider_order(self):
        assert any(
            isinstance(p, ProjectRunebergProvider) for p in PROVIDER_ORDER
        )

    def test_get_book_provider_by_name(self):
        assert isinstance(
            get_book_provider_by_name('runeberg'), ProjectRunebergProvider
        )

    def test_is_own_ocaid_substring_match(self):
        provider = ProjectRunebergProvider()
        # Substring, not just a prefix
        assert provider.is_own_ocaid('runeberg123')
        assert provider.is_own_ocaid('foo_runeberg_bar')
        assert provider.is_own_ocaid('some-runeberg')
        assert not provider.is_own_ocaid('gutenberg123')

    def test_get_acquisitions(self):
        provider = ProjectRunebergProvider()
        acquisitions = provider.get_acquisitions(
            {'identifiers': {'project_runeberg': ['abc123']}}
        )
        assert len(acquisitions) == 1
        acq = acquisitions[0]
        assert acq.access == 'open-access'
        assert acq.format == 'web'
        assert acq.url == 'https://runeberg.org/abc123/'
        assert acq.provider_name == 'runeberg'
