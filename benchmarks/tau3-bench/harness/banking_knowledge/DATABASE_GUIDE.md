# Database Query Guide

This guide shows how to write custom database functions using `db_query.py`.

## Basic Usage

```python
from harness.banking_knowledge.db_query import query_db, add_to_db
from harness.banking_knowledge.environment import get_db

db = get_db()
```

## Writing Custom Functions

### Simple Query Wrapper

```python
def get_credit_card_applications_by_name(customer_name: str):
    return query_db(
        "credit_card_applications",
        db=db,
        customer_name=customer_name,
    )

# Usage:
apps = get_credit_card_applications_by_name("Jane Customer")
```

### With Comparison Operators

```python
def get_high_value_transactions(min_amount: float):
    return query_db(
        "credit_card_transaction_history",
        db=db,
        transaction_amount__gt=min_amount,
    )

def get_pending_applications(card_type: str):
    return query_db(
        "credit_card_applications",
        db=db,
        card_type=card_type,
        status="PENDING",
    )
```

### As a Tool (for LLM use)

```python
from harness.banking_knowledge.db_query import query_database_tool

def get_user_information_tool(user_id: str) -> str:
    return query_database_tool(
        "users",
        f'{{"user_id": "{user_id}"}}',
        db=db,
    )
```

## Available Operators

| Operator | Example | Meaning |
|----------|---------|---------|
| (none) | `status="active"` | Exact match |
| `__gt` | `amount__gt=100` | Greater than |
| `__gte` | `amount__gte=100` | Greater than or equal |
| `__lt` | `amount__lt=100` | Less than |
| `__lte` | `amount__lte=100` | Less than or equal |
| `__ne` | `status__ne="closed"` | Not equal |
| `__contains` | `name__contains="john"` | Substring match |
| `__startswith` | `name__startswith="J"` | Starts with |
| `__endswith` | `email__endswith="@gmail.com"` | Ends with |
| `__in` | `status__in=["a","b"]` | Value in list |
| `__nin` | `status__nin=["x","y"]` | Value not in list |
