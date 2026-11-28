def _make_future_sle_dispatcher(original, stock_ledger):
    def dispatch(args):
        if can_skip_future_update(args):
            return None
        return original(args)

    dispatch._khitmee_dispatcher = True
    return dispatch
