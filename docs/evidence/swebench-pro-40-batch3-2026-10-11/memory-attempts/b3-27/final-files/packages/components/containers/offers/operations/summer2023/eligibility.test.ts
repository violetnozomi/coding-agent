import { getUnixTime, subMonths } from 'date-fns';

import { APPS } from '@proton/shared/lib/constants';
import { ProtonConfig, UserModel } from '@proton/shared/lib/interfaces';

import isEligible from './eligibility';

describe('summer-2023 offer', () => {
    it('should not be available in Proton VPN settings', () => {
        const user = {
            isFree: true,
            canPay: true,
        } as UserModel;
        const protonConfig = {
            APP_NAME: APPS.PROTONVPN_SETTINGS,
        } as ProtonConfig;
        expect(
            isEligible({
                user,
                protonConfig,
            })
        ).toBe(false);
    });

    it('should be available in Proton Mail', () => {
        const user = {
            isFree: true,
            canPay: true,
        } as UserModel;
        const protonConfig = {
            APP_NAME: APPS.PROTONMAIL,
        } as ProtonConfig;
        expect(
            isEligible({
                user,
                protonConfig,
            })
        ).toBe(true);
    });

    it('should be available in Proton Calendar', () => {
        const user = {
            isFree: true,
            canPay: true,
        } as UserModel;
        const protonConfig = {
            APP_NAME: APPS.PROTONCALENDAR,
        } as ProtonConfig;
        expect(
            isEligible({
                user,
                protonConfig,
            })
        ).toBe(true);
    });

    it('should not be available for a user who canceled less than one month ago', () => {
        const user = {
            isFree: true,
            canPay: true,
        } as UserModel;
        const protonConfig = {
            APP_NAME: APPS.PROTONMAIL,
        } as ProtonConfig;
        const lastSubscriptionEnd = getUnixTime(new Date());
        expect(isEligible({ user, protonConfig, lastSubscriptionEnd })).toBe(false);
    });

    it('should not be available for a user who canceled exactly one month ago', () => {
        const user = {
            isFree: true,
            canPay: true,
        } as UserModel;
        const protonConfig = {
            APP_NAME: APPS.PROTONMAIL,
        } as ProtonConfig;
        const lastSubscriptionEnd = getUnixTime(subMonths(new Date(), 1));
        expect(isEligible({ user, protonConfig, lastSubscriptionEnd })).toBe(false);
    });

    it('should be available for a user who canceled more than one month ago', () => {
        const user = {
            isFree: true,
            canPay: true,
        } as UserModel;
        const protonConfig = {
            APP_NAME: APPS.PROTONMAIL,
        } as ProtonConfig;
        const lastSubscriptionEnd = getUnixTime(subMonths(new Date(), 2));
        expect(isEligible({ user, protonConfig, lastSubscriptionEnd })).toBe(true);
    });
});
