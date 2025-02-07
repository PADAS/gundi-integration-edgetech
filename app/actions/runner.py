from edgetech import EdgetechDownloadClient

class Configuration():

    def __init__(self):
        self.authorizeUrl = 'https://trap-tracker.auth0.com/authorize'
        self.tokenUrl = 'https://trap-tracker.auth0.com/oauth/token'
        self.clientId = 'b76t6LBIW23mMY2TlMSplgIKH9Cs2yns'
        self.redirectUrl = 'https://app.local'
        self.scope = 'offline_access database:dump openid profile email'
        self.apiBaseUrl = 'https://api-test.trap-tracker.com'
        self.v1Url = self.apiBaseUrl + '/v1'
        self.databaseDumpUrl = self.v1Url + '/database-dump/tasks'
        self.numGetRetry = 60

def main():
    # Call functions in edgetech library to get a token and download a database dump
    print("Running main with staging configuration")
    access_token = input("Enter access token if you have one, otherwise press enter: ")

    config = Configuration()

    edgetech_client = EdgetechDownloadClient()

    if not access_token:
        token = edgetech_client.get_token(config= config)
        print(f"Token: {token}")
        access_token = token['access_token']

    data = edgetech_client.download_edgetech_data(config= config, access_token= access_token)

    print(f"Data: {data}")
    

if __name__ == "__main__":
    main()