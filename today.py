import datetime
from dateutil import relativedelta
import requests
import os
from lxml import etree
import time
import hashlib


# GitHub GraphQL API authentication
HEADERS = {
    'authorization': 'token ' + os.environ['ACCESS_TOKEN']
}

# Supplied through GitHub Actions secrets
USER_NAME = os.environ['USER_NAME']

QUERY_COUNT = {
    'user_getter': 0,
    'follower_getter': 0,
    'graph_repos_stars': 0,
    'recursive_loc': 0,
    'graph_commits': 0,
    'loc_query': 0
}


def daily_readme(birthday):
    """
    Returns the length of time since the user's birthday.
    Example: '22 years, 0 months, 9 days'
    """
    diff = relativedelta.relativedelta(
        datetime.datetime.today(),
        birthday
    )

    return '{} {}, {} {}, {} {}{}'.format(
        diff.years,
        'year' + format_plural(diff.years),
        diff.months,
        'month' + format_plural(diff.months),
        diff.days,
        'day' + format_plural(diff.days),
        ' 🎂' if (diff.months == 0 and diff.days == 0) else ''
    )


def format_plural(unit):
    """
    Returns the plural suffix for a number.
    """
    return 's' if unit != 1 else ''


def simple_request(func_name, query, variables):
    """
    Returns a GitHub GraphQL request or raises an exception
    if the response does not succeed.
    """
    request = requests.post(
        'https://api.github.com/graphql',
        json={
            'query': query,
            'variables': variables
        },
        headers=HEADERS
    )

    if request.status_code == 200:
        return request

    raise Exception(
        func_name,
        ' has failed with a',
        request.status_code,
        request.text,
        QUERY_COUNT
    )


def graph_commits(start_date, end_date):
    """
    Uses GitHub's GraphQL API to return total contribution count.
    """
    query_count('graph_commits')

    query = '''
    query($start_date: DateTime!, $end_date: DateTime!, $login: String!) {
        user(login: $login) {
            contributionsCollection(from: $start_date, to: $end_date) {
                contributionCalendar {
                    totalContributions
                }
            }
        }
    }'''

    variables = {
        'start_date': start_date,
        'end_date': end_date,
        'login': USER_NAME
    }

    request = simple_request(
        graph_commits.__name__,
        query,
        variables
    )

    return int(
        request.json()['data']['user']
        ['contributionsCollection']
        ['contributionCalendar']
        ['totalContributions']
    )


def graph_repos_stars(
    count_type,
    owner_affiliation,
    cursor=None,
    add_loc=0,
    del_loc=0
):
    """
    Uses GitHub's GraphQL API to return repository,
    star, or contribution-related data.
    """
    query_count('graph_repos_stars')

    query = '''
    query (
        $owner_affiliation: [RepositoryAffiliation],
        $login: String!,
        $cursor: String
    ) {
        user(login: $login) {
            repositories(
                first: 100,
                after: $cursor,
                ownerAffiliations: $owner_affiliation
            ) {
                totalCount

                edges {
                    node {
                        ... on Repository {
                            nameWithOwner

                            stargazers {
                                totalCount
                            }
                        }
                    }
                }

                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''

    variables = {
        'owner_affiliation': owner_affiliation,
        'login': USER_NAME,
        'cursor': cursor
    }

    request = simple_request(
        graph_repos_stars.__name__,
        query,
        variables
    )

    if request.status_code == 200:

        if count_type == 'repos':
            return request.json()['data']['user']['repositories']['totalCount']

        elif count_type == 'stars':
            return stars_counter(
                request.json()['data']['user']['repositories']['edges']
            )


def recursive_loc(
    owner,
    repo_name,
    data,
    cache_comment,
    addition_total=0,
    deletion_total=0,
    my_commits=0,
    cursor=None
):
    """
    Uses GitHub's GraphQL API and cursor pagination
    to fetch repository commits and calculate LOC.
    """
    query_count('recursive_loc')

    query = '''
    query (
        $repo_name: String!,
        $owner: String!,
        $cursor: String
    ) {
        repository(name: $repo_name, owner: $owner) {

            defaultBranchRef {
                target {

                    ... on Commit {

                        history(
                            first: 100,
                            after: $cursor
                        ) {

                            totalCount

                            edges {
                                node {

                                    ... on Commit {

                                        committedDate

                                        author {
                                            user {
                                                id
                                            }
                                        }

                                        deletions
                                        additions
                                    }
                                }
                            }

                            pageInfo {
                                endCursor
                                hasNextPage
                            }
                        }
                    }
                }
            }
        }
    }'''

    variables = {
        'repo_name': repo_name,
        'owner': owner,
        'cursor': cursor
    }

    request = requests.post(
        'https://api.github.com/graphql',
        json={
            'query': query,
            'variables': variables
        },
        headers=HEADERS
    )

    if request.status_code == 200:

        repository = request.json()['data']['repository']

        if repository['defaultBranchRef'] is not None:
            return loc_counter_one_repo(
                owner,
                repo_name,
                data,
                cache_comment,
                repository['defaultBranchRef']['target']['history'],
                addition_total,
                deletion_total,
                my_commits
            )

        else:
            return 0

    force_close_file(data, cache_comment)

    if request.status_code == 403:
        raise Exception(
            'Too many requests in a short amount of time!\n'
            "You've hit the non-documented anti-abuse limit!"
        )

    raise Exception(
        'recursive_loc() has failed with a',
        request.status_code,
        request.text,
        QUERY_COUNT
    )


def loc_counter_one_repo(
    owner,
    repo_name,
    data,
    cache_comment,
    history,
    addition_total,
    deletion_total,
    my_commits
):
    """
    Counts additions, deletions, and commits authored by the user.
    """

    for node in history['edges']:

        if node['node']['author']['user'] == OWNER_ID:

            my_commits += 1

            addition_total += node['node']['additions']
            deletion_total += node['node']['deletions']

    if history['edges'] == [] or not history['pageInfo']['hasNextPage']:

        return (
            addition_total,
            deletion_total,
            my_commits
        )

    else:

        return recursive_loc(
            owner,
            repo_name,
            data,
            cache_comment,
            addition_total,
            deletion_total,
            my_commits,
            history['pageInfo']['endCursor']
        )


def loc_query(
    owner_affiliation,
    comment_size=0,
    force_cache=False,
    cursor=None,
    edges=[]
):
    """
    Queries all repositories accessible to the user and
    calculates lines of code.
    """
    query_count('loc_query')

    query = '''
    query (
        $owner_affiliation: [RepositoryAffiliation],
        $login: String!,
        $cursor: String
    ) {
        user(login: $login) {

            repositories(
                first: 60,
                after: $cursor,
                ownerAffiliations: $owner_affiliation
            ) {

                edges {

                    node {

                        ... on Repository {

                            nameWithOwner

                            defaultBranchRef {
                                target {

                                    ... on Commit {

                                        history {
                                            totalCount
                                        }
                                    }
                                }
                            }
                        }
                    }
                }

                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''

    variables = {
        'owner_affiliation': owner_affiliation,
        'login': USER_NAME,
        'cursor': cursor
    }

    request = simple_request(
        loc_query.__name__,
        query,
        variables
    )

    repositories = request.json()['data']['user']['repositories']

    if repositories['pageInfo']['hasNextPage']:

        edges += repositories['edges']

        return loc_query(
            owner_affiliation,
            comment_size,
            force_cache,
            repositories['pageInfo']['endCursor'],
            edges
        )

    else:

        return cache_builder(
            edges + repositories['edges'],
            comment_size,
            force_cache
        )


def cache_builder(
    edges,
    comment_size,
    force_cache,
    loc_add=0,
    loc_del=0
):
    """
    Checks repositories against the local cache.
    """

    cached = True

    filename = (
        'cache/' +
        hashlib.sha256(
            USER_NAME.encode('utf-8')
        ).hexdigest() +
        '.txt'
    )

    try:

        with open(filename, 'r') as f:
            data = f.readlines()

    except FileNotFoundError:

        data = []

        if comment_size > 0:

            for _ in range(comment_size):
                data.append(
                    'This line is a comment block. '
                    'Write whatever you want here.\n'
                )

        with open(filename, 'w') as f:
            f.writelines(data)

    if len(data) - comment_size != len(edges) or force_cache:

        cached = False

        flush_cache(
            edges,
            filename,
            comment_size
        )

        with open(filename, 'r') as f:
            data = f.readlines()

    cache_comment = data[:comment_size]

    data = data[comment_size:]

    for index in range(len(edges)):

        repo_hash, commit_count, *__ = data[index].split()

        if repo_hash == hashlib.sha256(
            edges[index]['node']['nameWithOwner'].encode('utf-8')
        ).hexdigest():

            try:

                current_commit_count = (
                    edges[index]['node']
                    ['defaultBranchRef']
                    ['target']
                    ['history']
                    ['totalCount']
                )

                if int(commit_count) != current_commit_count:

                    owner, repo_name = (
                        edges[index]['node']
                        ['nameWithOwner']
                        .split('/')
                    )

                    loc = recursive_loc(
                        owner,
                        repo_name,
                        data,
                        cache_comment
                    )

                    data[index] = (
                        repo_hash + ' ' +
                        str(current_commit_count) + ' ' +
                        str(loc[2]) + ' ' +
                        str(loc[0]) + ' ' +
                        str(loc[1]) + '\n'
                    )

            except TypeError:

                data[index] = (
                    repo_hash +
                    ' 0 0 0 0\n'
                )

    with open(filename, 'w') as f:

        f.writelines(cache_comment)
        f.writelines(data)

    for line in data:

        loc = line.split()

        loc_add += int(loc[3])
        loc_del += int(loc[4])

    return [
        loc_add,
        loc_del,
        loc_add - loc_del,
        cached
    ]


def flush_cache(edges, filename, comment_size):
    """
    Wipes the cache file while preserving comments.
    """

    with open(filename, 'r') as f:

        data = []

        if comment_size > 0:
            data = f.readlines()[:comment_size]

    with open(filename, 'w') as f:

        f.writelines(data)

        for node in edges:

            f.write(
                hashlib.sha256(
                    node['node']['nameWithOwner'].encode('utf-8')
                ).hexdigest()
                + ' 0 0 0 0\n'
            )


def force_close_file(data, cache_comment):
    """
    Saves partial cache data if an API request fails.
    """

    filename = (
        'cache/' +
        hashlib.sha256(
            USER_NAME.encode('utf-8')
        ).hexdigest() +
        '.txt'
    )

    with open(filename, 'w') as f:

        f.writelines(cache_comment)
        f.writelines(data)

    print(
        'There was an error while writing to the cache file. '
        'The file,',
        filename,
        'has had the partial data saved and closed.'
    )

def stars_counter(data):
    """
    Count total stars in repositories owned by me.
    Safely skips repository nodes that GitHub returns as null.
    """
    total_stars = 0

    for node in data:
        repository = node.get('node') if node else None

        if repository is None:
            continue

        stargazers = repository.get('stargazers')

        if stargazers is None:
            continue

        total_stars += stargazers.get('totalCount', 0) or 0

    return total_stars


def svg_overwrite(
    filename,
    age_data,
    commit_data,
    star_data,
    repo_data,
    contrib_data,
    follower_data,
    loc_data
):
    """
    Parses an SVG file and updates GitHub statistics.
    """

    tree = etree.parse(filename)

    root = tree.getroot()

    justify_format(
        root,
        'commit_data',
        commit_data,
        22
    )

    justify_format(
        root,
        'star_data',
        star_data,
        14
    )

    justify_format(
        root,
        'repo_data',
        repo_data,
        6
    )

    justify_format(
        root,
        'contrib_data',
        contrib_data
    )

    justify_format(
        root,
        'follower_data',
        follower_data,
        10
    )

    justify_format(
        root,
        'loc_data',
        loc_data[2],
        9
    )

    justify_format(
        root,
        'loc_add',
        loc_data[0]
    )

    justify_format(
        root,
        'loc_del',
        loc_data[1],
        7
    )

    tree.write(
        filename,
        encoding='utf-8',
        xml_declaration=True
    )


def justify_format(
    root,
    element_id,
    new_text,
    length=0
):
    """
    Updates an SVG element and adjusts the preceding dots.
    """

    if isinstance(new_text, int):
        new_text = f"{new_text:,}"

    new_text = str(new_text)

    find_and_replace(
        root,
        element_id,
        new_text
    )

    just_len = max(
        0,
        length - len(new_text)
    )

    if just_len <= 2:

        dot_map = {
            0: '',
            1: ' ',
            2: '. '
        }

        dot_string = dot_map[just_len]

    else:

        dot_string = (
            ' ' +
            ('.' * just_len) +
            ' '
        )

    find_and_replace(
        root,
        f"{element_id}_dots",
        dot_string
    )


def find_and_replace(
    root,
    element_id,
    new_text
):
    """
    Finds an element in the SVG and replaces its text.
    """

    element = root.find(
        f".//*[@id='{element_id}']"
    )

    if element is not None:
        element.text = new_text


def commit_counter(comment_size):
    """
    Counts commits using the repository cache.
    """

    total_commits = 0

    filename = (
        'cache/' +
        hashlib.sha256(
            USER_NAME.encode('utf-8')
        ).hexdigest() +
        '.txt'
    )

    with open(filename, 'r') as f:
        data = f.readlines()

    data = data[comment_size:]

    for line in data:
        total_commits += int(
            line.split()[2]
        )

    return total_commits


def user_getter(username):
    """
    Returns the GitHub account ID and creation date.
    """

    query_count('user_getter')

    query = '''
    query($login: String!) {

        user(login: $login) {

            id
            createdAt
        }
    }'''

    variables = {
        'login': username
    }

    request = simple_request(
        user_getter.__name__,
        query,
        variables
    )

    return (
        {
            'id': request.json()['data']['user']['id']
        },
        request.json()['data']['user']['createdAt']
    )


def follower_getter(username):
    """
    Returns the user's follower count.
    """

    query_count('follower_getter')

    query = '''
    query($login: String!) {

        user(login: $login) {

            followers {
                totalCount
            }
        }
    }'''

    request = simple_request(
        follower_getter.__name__,
        query,
        {
            'login': username
        }
    )

    return int(
        request.json()['data']['user']
        ['followers']
        ['totalCount']
    )


def query_count(funct_id):
    """
    Counts GitHub GraphQL API calls.
    """

    global QUERY_COUNT

    QUERY_COUNT[funct_id] += 1


def perf_counter(funct, *args):
    """
    Calculates function execution time.
    """

    start = time.perf_counter()

    funct_return = funct(*args)

    return (
        funct_return,
        time.perf_counter() - start
    )


def formatter(
    query_type,
    difference,
    funct_return=False,
    whitespace=0
):
    """
    Prints formatted execution time.
    """

    print(
        '{:<23}'.format(
            '   ' + query_type + ':'
        ),
        sep='',
        end=''
    )

    if difference > 1:

        print(
            '{:>12}'.format(
                '%.4f' % difference + ' s '
            )
        )

    else:

        print(
            '{:>12}'.format(
                '%.4f' % (difference * 1000) + ' ms'
            )
        )

    if whitespace:

        return f"{'{:,}'.format(funct_return): <{whitespace}}"

    return funct_return


if __name__ == '__main__':

    print('Calculation times:')

    # Get the current user's GitHub account ID
    user_data, user_time = perf_counter(
        user_getter,
        USER_NAME
    )

    OWNER_ID, acc_date = user_data

    formatter(
        'account data',
        user_time
    )

    # Calculate age from 6 September 2004
    age_data, age_time = perf_counter(
        daily_readme,
        datetime.datetime(2004, 9, 6)
    )

    formatter(
        'age calculation',
        age_time
    )

    # Calculate lines of code
    total_loc, loc_time = perf_counter(
        loc_query,
        [
            'OWNER',
            'COLLABORATOR',
            'ORGANIZATION_MEMBER'
        ],
        7
    )

    if total_loc[-1]:

        formatter(
            'LOC (cached)',
            loc_time
        )

    else:

        formatter(
            'LOC (no cache)',
            loc_time
        )

    # Calculate commits
    commit_data, commit_time = perf_counter(
        commit_counter,
        7
    )

    # Calculate stars
    star_data, star_time = perf_counter(
        graph_repos_stars,
        'stars',
        ['OWNER']
    )

    # Calculate repositories
    repo_data, repo_time = perf_counter(
        graph_repos_stars,
        'repos',
        ['OWNER']
    )

    # Calculate contributions
    contrib_data, contrib_time = perf_counter(
        graph_repos_stars,
        'repos',
        [
            'OWNER',
            'COLLABORATOR',
            'ORGANIZATION_MEMBER'
        ]
    )

    # Calculate followers
    follower_data, follower_time = perf_counter(
        follower_getter,
        USER_NAME
    )

    # Update both SVG themes
    svg_overwrite(
        'dark_mode.svg',
        age_data,
        commit_data,
        star_data,
        repo_data,
        contrib_data,
        follower_data,
        total_loc[:-1]
    )

    svg_overwrite(
        'light_mode.svg',
        age_data,
        commit_data,
        star_data,
        repo_data,
        contrib_data,
        follower_data,
        total_loc[:-1]
    )

    print(
        'Total GitHub GraphQL API calls:',
        '{:>3}'.format(
            sum(QUERY_COUNT.values())
        )
    )

    for funct_name, count in QUERY_COUNT.items():

        print(
            '{:<28}'.format(
                '   ' + funct_name + ':'
            ),
            '{:>6}'.format(count)
        )