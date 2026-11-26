#import <UIKit/UIKit.h>
#import <AVFoundation/AVFoundation.h>
#import <math.h>

static void Put16(NSMutableData *data, uint16_t value) {
  uint16_t little = CFSwapInt16HostToLittle(value);
  [data appendBytes:&little length:sizeof(little)];
}
static void Put32(NSMutableData *data, uint32_t value) {
  uint32_t little = CFSwapInt32HostToLittle(value);
  [data appendBytes:&little length:sizeof(little)];
}
static NSData *TestTone(void) {
  const uint32_t rate = 48000, frames = rate * 5, bytes = frames * 4;
  NSMutableData *data = [NSMutableData dataWithCapacity:44 + bytes];
  [data appendBytes:"RIFF" length:4]; Put32(data, bytes + 36);
  [data appendBytes:"WAVEfmt " length:8]; Put32(data, 16);
  Put16(data, 1); Put16(data, 2); Put32(data, rate); Put32(data, rate * 4);
  Put16(data, 4); Put16(data, 16); [data appendBytes:"data" length:4]; Put32(data, bytes);
  for (uint32_t index = 0; index < frames; index++) {
    double edge = fmin(1.0, fmin(index / 480.0, (frames - index - 1) / 480.0));
    int16_t sample = (int16_t)(3276.0 * edge * sin(2.0 * M_PI * 440.0 * index / rate));
    Put16(data, (uint16_t)sample); Put16(data, (uint16_t)sample);
  }
  return data;
}

@interface CheckController : UIViewController <UITextFieldDelegate, UIGestureRecognizerDelegate, AVAudioPlayerDelegate>
@property(nonatomic, strong) UILabel *counter;
@property(nonatomic, strong) UILabel *echo;
@property(nonatomic, strong) UILabel *gestures;
@property(nonatomic, strong) UILabel *audioState;
@property(nonatomic, strong) AVAudioPlayer *player;
@property(nonatomic) NSInteger taps;
@property(nonatomic) NSInteger longPresses;
@property(nonatomic) NSInteger drags;
@property(nonatomic) NSInteger pinches;
@end

@implementation CheckController
- (UILabel *)label:(NSString *)text identifier:(NSString *)identifier {
  UILabel *label = [UILabel new]; label.text = text; label.numberOfLines = 0;
  label.font = [UIFont preferredFontForTextStyle:UIFontTextStyleBody];
  label.accessibilityIdentifier = identifier; return label;
}
- (UIButton *)button:(NSString *)title identifier:(NSString *)identifier action:(SEL)action {
  UIButton *button = [UIButton buttonWithType:UIButtonTypeSystem];
  [button setTitle:title forState:UIControlStateNormal]; button.accessibilityIdentifier = identifier;
  [button addTarget:self action:action forControlEvents:UIControlEventTouchUpInside];
  [button.heightAnchor constraintEqualToConstant:48].active = YES; return button;
}
- (void)viewDidLoad {
  [super viewDidLoad]; self.view.backgroundColor = UIColor.systemBackgroundColor;
  UIScrollView *scroll = [UIScrollView new]; scroll.translatesAutoresizingMaskIntoConstraints = NO;
  [self.view addSubview:scroll]; UIStackView *stack = [UIStackView new];
  stack.axis = UILayoutConstraintAxisVertical; stack.spacing = 12; stack.translatesAutoresizingMaskIntoConstraints = NO;
  [scroll addSubview:stack];
  [NSLayoutConstraint activateConstraints:@[
    [scroll.topAnchor constraintEqualToAnchor:self.view.safeAreaLayoutGuide.topAnchor],
    [scroll.bottomAnchor constraintEqualToAnchor:self.view.safeAreaLayoutGuide.bottomAnchor],
    [scroll.leadingAnchor constraintEqualToAnchor:self.view.leadingAnchor],
    [scroll.trailingAnchor constraintEqualToAnchor:self.view.trailingAnchor],
    [stack.topAnchor constraintEqualToAnchor:scroll.contentLayoutGuide.topAnchor constant:12],
    [stack.bottomAnchor constraintEqualToAnchor:scroll.contentLayoutGuide.bottomAnchor constant:-12],
    [stack.leadingAnchor constraintEqualToAnchor:scroll.contentLayoutGuide.leadingAnchor constant:20],
    [stack.trailingAnchor constraintEqualToAnchor:scroll.contentLayoutGuide.trailingAnchor constant:-20],
    [stack.widthAnchor constraintEqualToAnchor:scroll.frameLayoutGuide.widthAnchor constant:-40]
  ]];
  [stack addArrangedSubview:[self label:@"iPhone Agent Checks" identifier:@"AgentCheck.Title"]];
  self.counter = [self label:@"Taps: 0" identifier:@"AgentCheck.Counter"];
  [stack addArrangedSubview:self.counter];
  [stack addArrangedSubview:[self button:@"Increment" identifier:@"AgentCheck.Increment" action:@selector(increment)]];
  UITextField *input = [UITextField new]; input.borderStyle = UITextBorderStyleRoundedRect;
  input.placeholder = @"Type test text"; input.accessibilityIdentifier = @"AgentCheck.Input";
  input.autocorrectionType = UITextAutocorrectionTypeNo; input.autocapitalizationType = UITextAutocapitalizationTypeNone;
  input.delegate = self; [input addTarget:self action:@selector(inputChanged:) forControlEvents:UIControlEventEditingChanged];
  [input.heightAnchor constraintEqualToConstant:44].active = YES; [stack addArrangedSubview:input];
  self.echo = [self label:@"Echo: " identifier:@"AgentCheck.Echo"]; [stack addArrangedSubview:self.echo];
  [stack addArrangedSubview:[self button:@"Play five-second test tone" identifier:@"AgentCheck.PlayTone" action:@selector(playTone)]];
  [stack addArrangedSubview:[self button:@"Stop test tone" identifier:@"AgentCheck.StopTone" action:@selector(stopTone)]];
  self.audioState = [self label:@"Audio: idle" identifier:@"AgentCheck.AudioState"]; [stack addArrangedSubview:self.audioState];
  UIView *pad = [UIView new]; pad.backgroundColor = UIColor.systemTealColor;
  pad.isAccessibilityElement = YES; pad.accessibilityLabel = @"Gesture pad"; pad.accessibilityIdentifier = @"AgentCheck.GesturePad";
  [pad.heightAnchor constraintEqualToConstant:150].active = YES; [stack addArrangedSubview:pad];
  UILongPressGestureRecognizer *hold = [[UILongPressGestureRecognizer alloc] initWithTarget:self action:@selector(held:)];
  hold.minimumPressDuration = 0.5; hold.delegate = self; [pad addGestureRecognizer:hold];
  UIPanGestureRecognizer *pan = [[UIPanGestureRecognizer alloc] initWithTarget:self action:@selector(dragged:)];
  pan.delegate = self; [pad addGestureRecognizer:pan];
  UIPinchGestureRecognizer *pinch = [[UIPinchGestureRecognizer alloc] initWithTarget:self action:@selector(pinched:)];
  pinch.delegate = self; [pad addGestureRecognizer:pinch];
  self.gestures = [self label:@"Long presses: 0; drags: 0; pinches: 0" identifier:@"AgentCheck.Gestures"];
  [stack addArrangedSubview:self.gestures];
  for (NSInteger row = 1; row <= 20; row++) {
    [stack addArrangedSubview:[self label:[NSString stringWithFormat:@"Test row %ld", (long)row]
                                            identifier:[NSString stringWithFormat:@"AgentCheck.Row.%ld", (long)row]]];
  }
  NSURL *documents = [NSFileManager.defaultManager URLsForDirectory:NSDocumentDirectory inDomains:NSUserDomainMask].firstObject;
  NSError *error = nil;
  [@"iPhone Agent fixture\n" writeToURL:[documents URLByAppendingPathComponent:@"fixture.txt"]
                            atomically:YES encoding:NSUTF8StringEncoding error:&error];
  if (error) [stack addArrangedSubview:[self label:@"Fixture file unavailable" identifier:@"AgentCheck.FileError"]];
}
- (void)increment { self.taps++; self.counter.text = [NSString stringWithFormat:@"Taps: %ld", (long)self.taps]; }
- (void)inputChanged:(UITextField *)input { self.echo.text = [@"Echo: " stringByAppendingString:input.text ?: @""]; }
- (BOOL)textFieldShouldReturn:(UITextField *)textField { [textField resignFirstResponder]; return YES; }
- (void)updateGestures {
  self.gestures.text = [NSString stringWithFormat:@"Long presses: %ld; drags: %ld; pinches: %ld", (long)self.longPresses, (long)self.drags, (long)self.pinches];
}
- (void)held:(UILongPressGestureRecognizer *)gesture { if (gesture.state == UIGestureRecognizerStateBegan) { self.longPresses++; [self updateGestures]; } }
- (void)dragged:(UIPanGestureRecognizer *)gesture { if (gesture.state == UIGestureRecognizerStateEnded) { self.drags++; [self updateGestures]; } }
- (void)pinched:(UIPinchGestureRecognizer *)gesture { if (gesture.state == UIGestureRecognizerStateEnded) { self.pinches++; [self updateGestures]; } }
- (BOOL)gestureRecognizer:(UIGestureRecognizer *)first shouldRecognizeSimultaneouslyWithGestureRecognizer:(UIGestureRecognizer *)second { return YES; }
- (void)playTone {
  [self stopTone]; NSError *error = nil;
  AVAudioSession *session = AVAudioSession.sharedInstance;
  if (![session setCategory:AVAudioSessionCategoryPlayback withOptions:AVAudioSessionCategoryOptionMixWithOthers error:&error]
      || ![session setActive:YES error:&error]) { self.audioState.text = @"Audio: session error"; return; }
  self.player = [[AVAudioPlayer alloc] initWithData:TestTone() error:&error];
  self.player.volume = 0.25; self.player.delegate = self;
  if (!self.player || ![self.player play]) { [self stopTone]; self.audioState.text = @"Audio: player error"; return; }
  self.audioState.text = @"Audio: playing 440 Hz";
}
- (void)stopTone {
  [self.player stop]; self.player = nil;
  [AVAudioSession.sharedInstance setActive:NO withOptions:AVAudioSessionSetActiveOptionNotifyOthersOnDeactivation error:nil];
  self.audioState.text = @"Audio: idle";
}
- (void)audioPlayerDidFinishPlaying:(AVAudioPlayer *)player successfully:(BOOL)flag { [self stopTone]; }
- (UIInterfaceOrientationMask)supportedInterfaceOrientations { return UIInterfaceOrientationMaskAllButUpsideDown; }
@end

@interface CheckScene : UIResponder <UIWindowSceneDelegate>
@property(nonatomic, strong) UIWindow *window;
@end
@implementation CheckScene
- (void)scene:(UIScene *)scene willConnectToSession:(UISceneSession *)session options:(UISceneConnectionOptions *)options {
  if (![scene isKindOfClass:UIWindowScene.class]) return;
  self.window = [[UIWindow alloc] initWithWindowScene:(UIWindowScene *)scene];
  self.window.rootViewController = [CheckController new]; [self.window makeKeyAndVisible];
}
- (void)sceneDidEnterBackground:(UIScene *)scene { [(CheckController *)self.window.rootViewController stopTone]; }
@end
@interface CheckApp : UIResponder <UIApplicationDelegate>
@end
@implementation CheckApp
- (BOOL)application:(UIApplication *)application didFinishLaunchingWithOptions:(NSDictionary *)options { return YES; }
@end
int main(int argc, char *argv[]) { @autoreleasepool { return UIApplicationMain(argc, argv, nil, NSStringFromClass(CheckApp.class)); } }
