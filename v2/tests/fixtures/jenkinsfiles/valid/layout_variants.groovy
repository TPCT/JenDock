pipeline
{
    agent
    {
        docker
        {
            image 'node:22'
        }
    }
    environment
    {
        TOKEN = credentials(
            'NPM_TOKEN'
        )
    }
    stages
    {
        stage('Build')
        {
            steps
            {
                timeout(5) { sh 'npm ci' }
            }
        }
    }
}
