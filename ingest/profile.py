"""Legacy prose profiles have unknown lineage; no automatic preference promotion."""
def run(db,*,lookback_days=90,min_bookmarks=5):
    return {'updated':False,'reason':'general_lineage_required'}
