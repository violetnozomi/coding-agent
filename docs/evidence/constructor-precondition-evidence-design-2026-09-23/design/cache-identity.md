# Cache identity proposal

If implemented later, semantic-review cache identity must include each
selected path, symbol, relation, current source hash, rendered-envelope hash,
span, and truncation/omission state. It should not include query duration or
an index counter when the selected semantic evidence is unchanged. Any source
or envelope change must produce a different identity.

