import { fromUnixTime, isBefore, subMonths } from 'date-fns';

import { APPS } from '@proton/shared/lib/constants';
import { isManagedExternally, isTrial } from '@proton/shared/lib/helpers/subscription';
import { ProtonConfig, Subscription, UserModel } from '@proton/shared/lib/interfaces';

interface Props {
    user: UserModel;
    subscription?: Subscription;
    protonConfig: ProtonConfig;
    lastSubscriptionEnd?: number;
}

/**
 * A user is considered free since at least one month when their most recent paid
 * subscription ended at least one month ago (or they never had one).
 * `lastSubscriptionEnd` is a Unix timestamp in seconds, compared against the current time.
 */
const isFreeSinceAtLeastOneMonth = (user: UserModel, lastSubscriptionEnd: number) => {
    if (!user.isFree) {
        return false;
    }

    return isBefore(fromUnixTime(lastSubscriptionEnd), subMonths(new Date(), 1));
};

const isEligible = ({ user, subscription, protonConfig, lastSubscriptionEnd = 0 }: Props) => {
    const isValidApp = protonConfig?.APP_NAME === APPS.PROTONMAIL || protonConfig?.APP_NAME === APPS.PROTONCALENDAR;

    if (!isValidApp) {
        return false;
    }

    if (!user.canPay) {
        return false;
    }

    if (user.isDelinquent) {
        return false;
    }

    if (isTrial(subscription)) {
        return true;
    }

    if (isManagedExternally(subscription)) {
        return false;
    }

    return isFreeSinceAtLeastOneMonth(user, lastSubscriptionEnd);
};

export default isEligible;
