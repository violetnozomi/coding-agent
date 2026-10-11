import { addDays } from 'date-fns';

import { MAILBOX_LABEL_IDS } from '@proton/shared/lib/constants';
import { UserModel } from '@proton/shared/lib/interfaces';

import { MessageState } from '../logic/messages/messagesTypes';
import { canSetExpiration, getExpirationTime, getMinExpirationTime } from './expiration';

describe('canSetExpiration', () => {
    const messageState = {
        data: {
            LabelIDs: ['label1', 'label2'],
        },
    } as MessageState;
    const incorrectMessageState = {
        data: {
            LabelIDs: [MAILBOX_LABEL_IDS.SPAM, MAILBOX_LABEL_IDS.TRASH],
        },
    } as MessageState;
    it('should return false if feature flag is false', () => {
        expect(canSetExpiration(false, { isFree: false } as UserModel, messageState)).toBe(false);
    });

    it('should return false if user is free', () => {
        expect(canSetExpiration(true, { isFree: true } as UserModel, messageState)).toBe(false);
    });

    it('should return true if feature flag is true and user is paid', () => {
        expect(canSetExpiration(true, { isFree: false } as UserModel, messageState)).toBe(true);
    });

    it('should return false if labelIDs contains spam or trash', () => {
        expect(canSetExpiration(true, { isFree: false } as UserModel, incorrectMessageState)).toBe(false);
    });
});

describe('getExpirationTime', () => {
    it('should return null if days is undefined', () => {
        expect(getExpirationTime(undefined)).toBe(null);
    });

    it('should return a Unix timestamp if days is > 0', () => {
        expect(getExpirationTime(addDays(new Date(), 1))).toBeGreaterThan(0);
    });
});

describe('getMinExpirationTime', () => {
    afterEach(() => {
        jest.useRealTimers();
    });

    it('should return undefined if the selected date is not today', () => {
        const fakeNow = new Date(2021, 0, 1, 9, 20, 0);
        jest.useFakeTimers().setSystemTime(fakeNow.getTime());

        const date = new Date(2021, 0, 5, 9, 20, 0);

        expect(getMinExpirationTime(date)).toBe(undefined);
    });

    it('should return the next 30 minute interval when time < XX:30', () => {
        const fakeNow = new Date(2021, 0, 1, 9, 20, 0);
        jest.useFakeTimers().setSystemTime(fakeNow.getTime());

        // 9:20 + 30 minutes = 9:50, next 30 minute interval strictly later than 9:50 is 10:00
        expect(getMinExpirationTime(fakeNow)).toEqual(new Date(2021, 0, 1, 10, 0, 0));
    });

    it('should return the current interval when it is already at least 30 minutes ahead', () => {
        const fakeNow = new Date(2021, 0, 1, 9, 0, 0);
        jest.useFakeTimers().setSystemTime(fakeNow.getTime());

        // 9:00 + 30 minutes = 9:30, which itself is a valid 30 minute interval
        expect(getMinExpirationTime(fakeNow)).toEqual(new Date(2021, 0, 1, 9, 30, 0));
    });

    it('should return a date with minutes normalized to 0 or 30', () => {
        const fakeNow = new Date(2021, 0, 1, 9, 37, 12);
        jest.useFakeTimers().setSystemTime(fakeNow.getTime());

        const minTimeDate = getMinExpirationTime(fakeNow);

        expect(minTimeDate).toBeDefined();
        expect([0, 30]).toContain(minTimeDate?.getMinutes());
        expect(minTimeDate?.getSeconds()).toBe(0);
    });

    it('should return a date strictly later than now and at least 30 minutes ahead', () => {
        const fakeNow = new Date(2021, 0, 1, 9, 45, 30);
        jest.useFakeTimers().setSystemTime(fakeNow.getTime());

        const minTimeDate = getMinExpirationTime(fakeNow);

        expect(minTimeDate).toBeDefined();
        expect((minTimeDate as Date).getTime()).toBeGreaterThan(fakeNow.getTime());
        expect((minTimeDate as Date).getTime()).toBeGreaterThanOrEqual(fakeNow.getTime() + 30 * 60 * 1000);
        expect(minTimeDate).toEqual(new Date(2021, 0, 1, 10, 30, 0));
    });
});
