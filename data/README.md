# The demo database

`public_transport.sql.gz` is a full dump of the RouteWise demo database at schema version 7: 3,013 passengers,
30 routes, 66 vehicles, 612 departures, 24,811 tickets, their audit trail and payment ledger, with every table, view,
routine, trigger, event and partition. All of it is demo data (the passengers are generated, the users are the three
demo accounts).

Restore it on a machine with MySQL 8 (from the project folder):

```bash
mysql -u root -p < schema.sql                                   # creates the app's database account and its rights
gunzip -c data/public_transport.sql.gz | mysql -u root -p       # replaces the database with the demo data
```

Without this file, `schema.sql` plus `python scripts/load_sample_data.py` builds an equivalent data set; the tickets are
then dated relative to the day you run it.
