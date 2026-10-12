/*
 * 字幕ととのえ 完了通知（ChronoFlow の ChronoFlowNotifier.m を元にした、LULU Apps の同系統のヘルパー）。
 *
 * AppleScript の display notification は AppleScript ランタイム名義で配信され、
 * アイコンを差し替えても通知のアイコンが変わらない。UNUserNotificationCenter から
 * このアプリ名義で出すと、このアプリのアイコン（字幕ととのえのアイコン）が使われる。
 *
 * 本文はファイルで受け取る。1行目=本文 2行目=タイトル 3行目=サブタイトル。
 * 置き場所: ~/Library/Application Support/Japanese Subtitle Polish/notification.txt
 * 引数で渡さないのは、`open --args` で argv が届かずダイアログが出た事故があるため（ChronoFlowでの経験）。
 */
#import <Foundation/Foundation.h>
#import <AppKit/AppKit.h>
#import <UserNotifications/UserNotifications.h>

@interface SubtitlePolishNotificationDelegate
    : NSObject <UNUserNotificationCenterDelegate>
@end

@implementation SubtitlePolishNotificationDelegate
- (void)userNotificationCenter:(UNUserNotificationCenter *)center
       willPresentNotification:(UNNotification *)notification
         withCompletionHandler:
             (void (^)(UNNotificationPresentationOptions options))completionHandler {
    /*
     * このヘルパーは通知の配信が完了するまで起動しているため、macOSから
     * 前面アプリ扱いされる。デリゲート未指定だとDBへ配信されても画面にも
     * 通知センターにも出ないので、バナーと通知リストへの表示を明示する。
     */
    completionHandler(
        UNNotificationPresentationOptionBanner |
        UNNotificationPresentationOptionList
    );
}
@end

static void WriteResult(NSString *path, NSString *status) {
    if (!path.length) return;
    NSString *safeStatus = [[status ?: @"unknown"
        stringByReplacingOccurrencesOfString:@"\n" withString:@" "]
        stringByAppendingString:@"\n"];
    [safeStatus writeToFile:path
                 atomically:YES
                   encoding:NSUTF8StringEncoding
                      error:nil];
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        NSApplication *application = [NSApplication sharedApplication];
        [application setActivationPolicy:NSApplicationActivationPolicyAccessory];
        NSString *payloadPath = [NSHomeDirectory()
            stringByAppendingPathComponent:
                @"Library/Application Support/Japanese Subtitle Polish/notification.txt"];
        NSString *resultPath = [NSHomeDirectory()
            stringByAppendingPathComponent:
                @"Library/Application Support/Japanese Subtitle Polish/notification-result.txt"];
        [[NSFileManager defaultManager] removeItemAtPath:resultPath error:nil];
        NSString *payload = [NSString stringWithContentsOfFile:payloadPath
                                                      encoding:NSUTF8StringEncoding
                                                         error:nil];
        if (!payload.length) {
            WriteResult(resultPath, @"invalid-payload");
            return 0;
        }
        NSArray<NSString *> *lines = [payload componentsSeparatedByString:@"\n"];
        if (lines.count < 3) {
            WriteResult(resultPath, @"invalid-payload");
            return 0;
        }

        UNUserNotificationCenter *center =
            [UNUserNotificationCenter currentNotificationCenter];
        SubtitlePolishNotificationDelegate *notificationDelegate =
            [[SubtitlePolishNotificationDelegate alloc] init];
        center.delegate = notificationDelegate;
        __block BOOL finished = NO;
        __block NSString *deliveryStatus = @"timeout";
        [center requestAuthorizationWithOptions:UNAuthorizationOptionAlert
                              completionHandler:^(BOOL granted, NSError *error) {
            if (!granted) {
                deliveryStatus = error
                    ? [@"error:" stringByAppendingString:error.localizedDescription]
                    : @"denied";
                finished = YES;
                return;
            }
            [center getNotificationSettingsWithCompletionHandler:
                ^(UNNotificationSettings *settings) {
                NSString *settingsSummary = [NSString stringWithFormat:
                    @"|authorization=%ld|alert=%ld|center=%ld|alertStyle=%ld",
                    (long)settings.authorizationStatus,
                    (long)settings.alertSetting,
                    (long)settings.notificationCenterSetting,
                    (long)settings.alertStyle];
                UNMutableNotificationContent *content =
                    [[UNMutableNotificationContent alloc] init];
                content.title = lines[1];
                content.subtitle = lines[2];
                content.body = lines[0];
                /*
                 * 即時要求(trigger=nil)はDBへ配信済みでもバナー/通知センターへ
                 * 表示されない実機があったため、1秒後の通常通知として登録する。
                 */
                UNTimeIntervalNotificationTrigger *trigger =
                    [UNTimeIntervalNotificationTrigger
                        triggerWithTimeInterval:1.0 repeats:NO];
                UNNotificationRequest *request =
                    [UNNotificationRequest
                        requestWithIdentifier:[[NSUUID UUID] UUIDString]
                                     content:content
                                     trigger:trigger];
                [center addNotificationRequest:request
                         withCompletionHandler:^(NSError *addError) {
                    deliveryStatus = addError
                        ? [@"error:" stringByAppendingString:
                            addError.localizedDescription]
                        : [@"accepted" stringByAppendingString:settingsSummary];
                    finished = YES;
                }];
            }];
        }];
        NSDate *deadline = [NSDate dateWithTimeIntervalSinceNow:5.0];
        while (!finished && [deadline timeIntervalSinceNow] > 0) {
            [[NSRunLoop currentRunLoop] runMode:NSDefaultRunLoopMode
                                     beforeDate:[NSDate dateWithTimeIntervalSinceNow:0.1]];
        }
        WriteResult(resultPath, deliveryStatus);
        /*
         * 1秒後の配信時にもメインRunLoopを動かし、willPresentNotificationを
         * 受け取れるようにする。sleepするとデリゲートが呼ばれず表示されない。
         */
        NSDate *presentationDeadline =
            [NSDate dateWithTimeIntervalSinceNow:8.0];
        while ([presentationDeadline timeIntervalSinceNow] > 0) {
            [[NSRunLoop currentRunLoop]
                runMode:NSDefaultRunLoopMode
                beforeDate:[NSDate dateWithTimeIntervalSinceNow:0.1]];
        }
    }
    return 0;
}
