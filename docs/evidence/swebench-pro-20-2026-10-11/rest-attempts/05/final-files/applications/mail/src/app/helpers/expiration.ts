import { addMinutes, getUnixTime, isToday } from 'date-fns';

import { UserModel } from '@proton/shared/lib/interfaces';
import { isFrozenExpiration } from '@proton/shared/lib/mail/messages';

import { MessageState } from '../logic/messages/messagesTypes';
import { isAllowedAutoDeleteLabelID } from './autoDelete';

export const canSetExpiration = (featureFlagValue: boolean, user: UserModel, messageState?: MessageState) => {
    const hasFrozenExpiration = isFrozenExpiration(messageState?.data);
    const { LabelIDs = [] } = messageState?.data || {};

    if (hasFrozenExpiration) {
        return false;
    }

    if (!featureFlagValue) {
        return false;
    }

    if (user.isFree) {
        return false;
    }

    if (!LabelIDs.length || LabelIDs.some((labelID) => isAllowedAutoDeleteLabelID(labelID))) {
        return false;
    }

    return true;
};

export const getExpirationTime = (date?: Date) => {
    return date ? getUnixTime(date) : null;
};

/**
 * Compute the minimum time that can be selected in the expiration time input for the provided date.
 *
 * Expiration times are aligned on 30 minute steps (XX:00 and XX:30). When the selected date is today,
 * the minimum is the next 30 minute step that is strictly later than now and at least 30 minutes ahead.
 * For any other day, there is no minimum constraint.
 */
export const getMinExpirationTime = (date: Date): Date | undefined => {
    // If the selected date is not today, we want to display all intervals, so there is no minimum.
    if (!isToday(date)) {
        return undefined;
    }

    // Expiration cannot happen sooner than 30 minutes from now.
    const limit = addMinutes(new Date(), 30);

    // 30 minute step (XX:00 or XX:30, seconds and milliseconds cleared) at or before the limit.
    const interval = new Date(limit);
    interval.setMinutes(limit.getMinutes() < 30 ? 0 : 30, 0, 0);

    // Keep the step only if it is not before the limit, otherwise take the next 30 minute step.
    return interval.getTime() >= limit.getTime() ? interval : addMinutes(interval, 30);
};
