"""Turn what a user typed into a search box into SQL conditions that can use an index.

Words go to the FULLTEXT indexes (MATCH ... AGAINST in boolean mode, each word as a required prefix), so a search reads
only the matching rows. LIKE '%word%' would read the whole table, because an index cannot help with a leading wildcard.
"""
import re

# InnoDB ignores these words and words shorter than innodb_ft_min_token_size (3), so they must not be required terms
STOPWORDS = {'about', 'are', 'com', 'for', 'from', 'how', 'that', 'the', 'this', 'was', 'what', 'when', 'where', 'who',
             'will', 'with', 'und', 'www'}
SEAT = re.compile(r'[1-9][0-9]{0,2}[A-Fa-f]')
VEHICLE = re.compile(r'[A-Za-z]{2}-[0-9A-Za-z-]*')


def words(q):
    return [w for w in re.findall(r'[^\W_]+', q) if len(w) >= 3 and w.lower() not in STOPWORDS]


def boolean_query(q):
    """'Ravi kum' -> '+Ravi* +kum*' (every word must start a word in the row), or None when no word is searchable."""
    found = words(q)
    return ' '.join(f'+{w}*' for w in found) if found else None


def like(q):
    return f'%{q}%'


def prefix(q):
    return q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
