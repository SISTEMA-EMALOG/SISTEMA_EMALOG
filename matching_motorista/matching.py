"""
Matching de motoristas — ranqueia motoristas elegíveis para um frete.
Extraído de utils/driver_bid_agent.py (antes misturado com a lógica de
negociação de OFERTA_FRETE_MOTORISTA) para que este módulo possa evoluir
e, futuramente, ser licenciado de forma independente.
"""
from infraestrutura_critica.models import Driver, Freight as FreightModel


def find_eligible_drivers(freight):
    """
    Return drivers eligible for a freight, ranked by relevance.
    Priority: 1) same route history  2) matching truck type + available  3) others available
    """
    # Already contacted drivers for this freight
    already_bid = {b.driver_id for b in freight.driver_bids}

    # Get required truck type from quote
    required_type = None
    if freight.quote:
        required_type = (freight.quote.vehicle_type or '').lower()

    all_drivers = Driver.query.filter_by(active=True, is_active=True).all()

    # Find drivers who've done the same route before
    route_veterans = set()
    if freight.origin_city and freight.destination_city:
        past = FreightModel.query.filter(
            FreightModel.origin_city == freight.origin_city,
            FreightModel.destination_city == freight.destination_city,
            FreightModel.assigned_driver_id.isnot(None)
        ).all()
        route_veterans = {f.assigned_driver_id for f in past}

    result = []
    for d in all_drivers:
        is_available = d.availability_status == 'disponivel'
        type_match   = (not required_type) or (d.truck_type or '').lower() == required_type
        is_veteran   = d.id in route_veterans
        has_bid      = d.id in already_bid

        result.append({
            'driver':       d,
            'type_match':   type_match,
            'is_available': is_available,
            'is_veteran':   is_veteran,
            'has_bid':      has_bid,
            'priority':     (3 if is_veteran else 0) + (2 if type_match else 0) + (1 if is_available else 0)
        })

    result.sort(key=lambda x: -x['priority'])
    return result
