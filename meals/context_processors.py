# Which top-level section each URL name belongs to, for highlighting the nav.
NAV_SECTIONS = {
    'index': 'week', 'plan': 'week', 'meal_detail': 'week',
    'groceries': 'groceries', 'current_groceries': 'groceries',
    'recipes': 'recipes', 'recipe_detail': 'recipes',
    'settings': 'settings',
}


def ui(request):
    theme = request.COOKIES.get('theme')
    match = request.resolver_match
    return {
        'theme': theme if theme in ('light', 'dark') else 'dark',
        'nav_section': NAV_SECTIONS.get(match.url_name if match else None, ''),
    }
