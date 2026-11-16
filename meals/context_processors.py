# Which top-level section each URL name belongs to, for highlighting the nav.
NAV_SECTIONS = {
    'index': 'week', 'plan': 'week', 'meal_detail': 'week',
    'groceries': 'groceries', 'current_groceries': 'groceries',
    'recipes': 'recipes', 'recipe_detail': 'recipes',
    'settings': 'settings',
    'preferences': 'preferences',
}


def ui(request):
    theme = request.COOKIES.get('theme')
    match = request.resolver_match
    member = getattr(request, 'member', None)  # set by views.HouseholdMixin
    return {
        'theme': theme if theme in ('light', 'dark') else 'dark',
        'nav_section': NAV_SECTIONS.get(match.url_name if match else None, ''),
        # The account menu's household switcher.
        'current_household': getattr(request, 'household', None),
        'my_households': list(member.households.all()) if member else [],
    }
