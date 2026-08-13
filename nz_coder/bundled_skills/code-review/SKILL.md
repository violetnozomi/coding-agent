---
name: code-review
description: Code review checklist and best practices
---
# Code Review Skill

When reviewing code, check these areas systematically:

## Security
- Input validation at system boundaries
- SQL injection / XSS prevention
- Secrets not hardcoded
- Proper authentication/authorization

## Correctness
- Edge cases handled (null, empty, overflow)
- Error handling present and appropriate
- Resource cleanup (files, connections, locks)

## Maintainability
- Clear naming conventions
- Single responsibility principle
- No unnecessary complexity
- Comments explain "why", not "what"

## Performance
- No N+1 queries
- Appropriate data structures
- No unnecessary copies/allocations
- Indexing for frequent queries

## Testing
- Unit tests cover core logic
- Edge cases tested
- Mocks used appropriately
- Tests are readable and maintainable
