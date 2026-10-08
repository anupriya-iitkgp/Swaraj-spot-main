"""The subsystem this project owns — the solid boxes of HLD §4.

One module per component in the HLD §6 responsibilities table, named the same,
so a reviewer can hold the document next to the tree and check them off.

The one structural departure from the reference design is the Grace Timer.
HLD §6 lists it as a component owning "the authoritative 120 s clock"; here the
clock is an absolute `force_stop_deadline` column and a database-backed reaper
in `spotd.workers.grace_reaper`. The responsibility is unchanged and the
authority is unchanged — what moved is where the countdown lives, because a
countdown held in a coroutine does not survive a restart (LLD §12.3).
"""

from .admission_controller import Admission, AdmissionController
from .eligibility_guard import EligibilityGuard, Validated
from .interruption_analytics import InterruptionAnalytics, SLOReport
from .lease_manager import SpotLeaseManager
from .metering import MeteringService, RatingResult
from .notice_delivery import NoticeDeliveryService, NoticeResult
from .placement_adapter import PlacementAdapter
from .pool_view import SpotPoolView
from .pricing import PricingEngine, Quote
from .provisioning_adapter import ProvisioningAdapter
from .reclaim_handler import ReclaimOrderHandler, ReclaimOutcome
from .spot_market_api import InventoryEntry, LaunchResult, SpotMarketAPI
from .teardown_confirmer import TeardownConfirmer, TeardownVerdict
from .victim_selector import Selection, VictimSelector

__all__ = [
    "Admission",
    "AdmissionController",
    "EligibilityGuard",
    "InterruptionAnalytics",
    "InventoryEntry",
    "LaunchResult",
    "MeteringService",
    "NoticeDeliveryService",
    "NoticeResult",
    "PlacementAdapter",
    "PricingEngine",
    "ProvisioningAdapter",
    "Quote",
    "RatingResult",
    "ReclaimOrderHandler",
    "ReclaimOutcome",
    "SLOReport",
    "Selection",
    "SpotLeaseManager",
    "SpotMarketAPI",
    "SpotPoolView",
    "TeardownConfirmer",
    "TeardownVerdict",
    "Validated",
    "VictimSelector",
]
